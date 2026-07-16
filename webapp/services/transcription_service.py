"""Live-transcription service for the SE Skills webapp.

Owns the in-memory session registry, audio capture lifecycle, transcript
persistence/recovery, saved-transcript filesystem access, and the context
helpers used by the live-session Ask path. Audio and Whisper libraries are
imported lazily inside capture methods so the app boots even when they are not
installed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import re
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import persistence
import security
from services.path_utils import resolve_within

logger = logging.getLogger(__name__)

TARGET_SR = 16000          # whisper wants 16k mono
WINDOW_SEC = 5.0           # transcribe ~5s windows
SILENCE_RMS = 0.004        # skip near-silent windows

# Echo de-dupe: when capturing with open speakers, the mic ("You") also hears
# the call's audio ("Call"), producing a near-duplicate line ~1s apart. We hold
# each "You" segment briefly; if a near-identical "Call" segment shows up in the
# window, the "You" line is an echo and is suppressed. "Call" emits immediately.
ECHO_HOLD_SEC = 2.5
ECHO_SIM = 0.5   # token-overlap to call two near-simultaneous lines an echo.
                 # 0.5 catches garbled echoes (the two channels transcribe a bit
                 # differently) while real back-and-forth dialogue scores ~0.1–0.3.


class TranscriptionError(Exception):
    """Domain exception carrying an HTTP-like status code and detail."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


@dataclass
class _PendingMicSegment:
    ts: float
    seg: dict[str, Any]


class _Channel:
    """One capture stream (sounddevice) → 16k mono windows → faster-whisper.

    `label` is the speaker tag for segments from this channel (e.g. You/Call).
    """

    def __init__(
        self,
        device_index: int,
        label: str,
        on_segment: Callable[[str, str], None],
        get_whisper: Callable[[], Any],
    ) -> None:
        import sounddevice as sd  # lazy
        import numpy as np

        self.np = np
        self.label = label
        self.on_segment = on_segment      # callback(label, text) — runs in worker thread
        self.get_whisper = get_whisper
        self._stop = threading.Event()
        self._q: queue.Queue = queue.Queue()
        info = sd.query_devices(device_index, "input")
        self.src_sr = int(info["default_samplerate"])
        self.in_ch = info["max_input_channels"]

        def cb(indata, frames, time_info, status):  # audio thread
            mono = indata.mean(axis=1) if indata.ndim > 1 and indata.shape[1] > 1 else indata.reshape(-1)
            if self.src_sr != TARGET_SR:
                n_out = max(1, int(len(mono) * TARGET_SR / self.src_sr))
                mono = np.interp(np.linspace(0, len(mono), n_out, endpoint=False),
                                 np.arange(len(mono)), mono).astype(np.float32)
            self._q.put(mono.astype(np.float32))

        self._stream = sd.InputStream(device=device_index, channels=self.in_ch,
                                      samplerate=self.src_sr, dtype="float32",
                                      callback=cb, blocksize=int(self.src_sr * 0.1))
        self._worker = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._stream.start()
        self._worker.start()

    def _run(self) -> None:
        model = self.get_whisper()
        buf: deque = deque()
        buflen = 0
        need = int(TARGET_SR * WINDOW_SEC)
        while not self._stop.is_set() or not self._q.empty():
            try:
                buf.append(self._q.get(timeout=0.3))
                buflen += len(buf[-1])
            except queue.Empty:
                pass
            if buflen >= need:
                window = self.np.concatenate(list(buf))
                buf.clear()
                buflen = 0
                if float(self.np.sqrt(self.np.mean(window ** 2))) < SILENCE_RMS:
                    continue
                try:
                    segs, _ = model.transcribe(window, language="en", beam_size=1)
                    text = " ".join(s.text.strip() for s in segs).strip()
                except Exception:
                    text = ""
                if text:
                    self.on_segment(self.label, text)

    def stop(self) -> None:
        self._stop.set()
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass


def _text_similarity(a: str, b: str) -> float:
    """Jaccard overlap of lowercased word sets — cheap, good enough for echoes."""
    wa = set(re.findall(r"[a-z0-9]+", (a or "").lower()))
    wb = set(re.findall(r"[a-z0-9]+", (b or "").lower()))
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


class LiveSession:
    """One live-transcription capture session: channels, segments, persistence."""

    def __init__(
        self,
        account: str,
        opp_slug: str | None,
        mic_device: int,
        call_device: int | None = None,
        mic_label: str = "You",
        call_label: str = "Call",
        opportunity: str | None = None,
        recovered: bool = False,
        segments: list[dict[str, Any]] | None = None,
        started_at: datetime | None = None,
        session_id: str | None = None,
        persist_fn: Callable[[dict[str, Any]], bool] | None = None,
        get_whisper: Callable[[], Any] | None = None,
    ) -> None:
        self.session_id = session_id
        self.account = account
        self.opp_slug = opp_slug
        self.opportunity = opportunity
        self.mic_device = mic_device
        self.call_device = call_device
        self.mic_label = (mic_label or "You").strip() or "You"
        self.call_label = (call_label or "Call").strip() or "Call"
        self.labeled = call_device is not None
        self.recovered = recovered
        self.ended = recovered  # recovered sessions are no longer capturing audio
        self.started_at = started_at or datetime.now(timezone.utc)
        self.persistence_warning: str | None = None
        self.segments: list[dict] = list(segments or [])
        self.queue: asyncio.Queue = asyncio.Queue()
        self._lock = threading.Lock()
        self._recent_call: deque = deque(maxlen=12)   # (monotonic_ts, text) for echo matching
        self._pending_you: list[_PendingMicSegment] = []  # held mic segs awaiting echo check
        self.channels: list[_Channel] = []
        self.persist_fn = persist_fn
        self.get_whisper = get_whisper
        if recovered:
            try:
                self.loop = asyncio.get_running_loop()
            except RuntimeError:
                self.loop = None
        else:
            self.loop = asyncio.get_running_loop()
            self.__post_channels(mic_device, call_device)

    def _emit(self, seg: dict[str, Any]) -> None:
        self.segments.append(seg)
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, seg)
        self._persist()

    def _persist(self) -> None:
        if self.session_id and self.persist_fn:
            ok = self.persist_fn(self.to_dict())
            if ok:
                self.persistence_warning = None
            else:
                self.persistence_warning = "Live transcript will not survive a server restart because state could not be saved."

    def _flush_pending_you(self, now: float) -> None:
        """Emit any held mic segments older than the hold window that were not
        matched by a call echo."""
        still: list[_PendingMicSegment] = []
        for ts, seg in self._pending_you:
            if now - ts >= ECHO_HOLD_SEC:
                self._emit(seg)
            else:
                still.append(_PendingMicSegment(ts, seg))
        self._pending_you = still

    def _on_segment(self, label: str, text: str) -> None:  # called from worker threads
        import time
        now = time.monotonic()
        seg = {"t": datetime.now(timezone.utc).strftime("%H:%M:%S"), "speaker": label, "text": text}
        with self._lock:
            self._flush_pending_you(now)
            if not self.labeled:
                self._emit(seg)
                return
            if label == self.call_label:
                # drop any held mic segment that this call line echoes
                self._pending_you = [
                    item for item in self._pending_you
                    if _text_similarity(item.seg["text"], text) < ECHO_SIM
                ]
                self._recent_call.append((now, text))
                self._emit(seg)
            else:  # mic label — suppress if it echoes a recent call; else hold briefly
                if any(
                    now - cts <= ECHO_HOLD_SEC and _text_similarity(text, ctext) >= ECHO_SIM
                    for cts, ctext in self._recent_call
                ):
                    return  # echo of the call audio — drop
                self._pending_you.append(_PendingMicSegment(now, seg))

    def __post_channels(self, mic_device: int, call_device: int | None) -> None:
        if self.labeled:
            self.channels.append(_Channel(mic_device, self.mic_label, self._on_segment, self.get_whisper))
            self.channels.append(_Channel(call_device, self.call_label, self._on_segment, self.get_whisper))
        else:
            self.channels.append(_Channel(mic_device, self.mic_label, self._on_segment, self.get_whisper))

    def start(self) -> None:
        for c in self.channels:
            c.start()

    def stop(self) -> None:
        for c in self.channels:
            c.stop()
        # flush any held mic segments that never got an echo match
        with self._lock:
            for _ts, seg in self._pending_you:
                self._emit(seg)
            self._pending_you = []

    def transcript_text(self) -> str:
        header = f"# Live transcript — {self.account} — {self.started_at.astimezone().strftime('%B %d, %Y %H:%M')}\n"
        header += f"# mic-label: {self.mic_label}\n"
        if self.labeled:
            header += f"# call-label: {self.call_label}\n"
        header += "\n"
        lines = []
        for s in self.segments:
            who = f"{s['speaker']}: " if s["speaker"] else ""
            lines.append(f"[{s['t']}] {who}{s['text']}")
        return header + "\n".join(lines) + "\n"

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "account": self.account,
            "opp_slug": self.opp_slug,
            "opportunity": self.opportunity,
            "mic_device": self.mic_device,
            "call_device": self.call_device,
            "mic_label": self.mic_label,
            "call_label": self.call_label,
            "labeled": self.labeled,
            "recovered": self.recovered,
            "ended": self.ended,
            "started_at": self.started_at.timestamp(),
            "segments": list(self.segments),
        }

    @classmethod
    def from_state(
        cls,
        data: dict[str, Any],
        *,
        persist_fn: Callable[[dict[str, Any]], bool] | None = None,
        get_whisper: Callable[[], Any] | None = None,
    ) -> "LiveSession":
        started_at = datetime.fromtimestamp(data["started_at"], tz=timezone.utc)
        return cls(
            account=data["account"],
            opp_slug=data.get("opp_slug"),
            mic_device=data.get("mic_device", 0),
            call_device=data.get("call_device"),
            mic_label=data.get("mic_label", "You"),
            call_label=data.get("call_label", "Call"),
            opportunity=data.get("opportunity"),
            recovered=True,
            segments=data.get("segments", []),
            started_at=started_at,
            session_id=data.get("session_id"),
            persist_fn=persist_fn,
            get_whisper=get_whisper,
        )


def _parse_saved_transcript(text: str) -> dict[str, Any]:
    """Parse a saved transcript file back into segments and speaker labels.

    Saved format (one per line): `[HH:MM:SS] Speaker: text`. Leading `#` header
    lines are skipped; `# mic-label:` and `# call-label:` set the expected
    speaker names (falling back to `You` / `Call`). Lines that don't match the
    pattern are appended to the previous segment (whisper sometimes wraps long
    utterances). A colon in the body text is protected by only accepting a known
    speaker label as the prefix.
    """
    mic_label, call_label = "You", "Call"
    segs: list[dict] = []
    line_re = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]\s*(?:([^\n:]+?):\s)?(.*)$")
    label_re = re.compile(r"^#\s*(mic-label|call-label):\s*(.+)$", re.IGNORECASE)
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            m = label_re.match(line)
            if m:
                value = m.group(2).strip()
                if m.group(1).lower() == "mic-label":
                    mic_label = value or mic_label
                else:
                    call_label = value or call_label
            continue
        m = line_re.match(line)
        if m:
            speaker = (m.group(2) or "").strip()
            body = m.group(3)
            if speaker and speaker not in {mic_label, call_label}:
                # the prefix was text, not a speaker label
                body = f"{speaker}: {body}"
                speaker = ""
            segs.append({"t": m.group(1), "speaker": speaker, "text": body})
        elif segs:
            segs[-1]["text"] += " " + line
    return {"segments": segs, "mic_label": mic_label, "call_label": call_label}


class TranscriptionService:
    """Cohesive live-transcription lifecycle and saved-transcript access."""

    def __init__(
        self,
        customers_dir: Path,
        workspace: Path,
        *,
        safe_name: Callable[[str], str],
        titlecase: Callable[[str], str],
        whisper_model: str | None = None,
    ) -> None:
        self.customers_dir = customers_dir
        self.workspace = workspace
        self.safe_name = safe_name
        self.titlecase = titlecase
        self.whisper_model = whisper_model or os.environ.get("SE_WHISPER_MODEL", "small")
        self.sessions: dict[str, LiveSession] = {}
        self._whisper: Any | None = None
        self._recover_sessions()

    # ------------------------------------------------------------------
    # Whisper model
    # ------------------------------------------------------------------
    def _get_whisper(self) -> Any:
        if self._whisper is None:
            from faster_whisper import WhisperModel  # lazy
            self._whisper = WhisperModel(self.whisper_model, device="cpu", compute_type="int8")
        return self._whisper

    # ------------------------------------------------------------------
    # Session registry
    # ------------------------------------------------------------------
    def _save_session(self, data: dict[str, Any]) -> bool:
        return persistence.save_session(data, self.workspace)

    def _recover_sessions(self) -> None:
        for data in persistence.load_sessions(self.workspace):
            try:
                sess = LiveSession.from_state(
                    data,
                    persist_fn=self._save_session,
                    get_whisper=self._get_whisper,
                )
                if sess.session_id:
                    self.sessions[sess.session_id] = sess
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------
    def audio_devices(self) -> dict[str, Any]:
        """Input devices for the mic/call pickers. Flags BlackHole presence."""
        try:
            import sounddevice as sd
        except Exception as e:  # noqa: BLE001
            raise TranscriptionError(
                500,
                security.redact_sensitive(
                    f"Audio capture unavailable: {e}. Run `brew install portaudio` and reinstall deps."
                ),
            ) from e
        devices, has_blackhole = [], False
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0:
                name = d["name"]
                if "blackhole" in name.lower() or "aggregate" in name.lower():
                    has_blackhole = True
                devices.append({
                    "index": i,
                    "name": name,
                    "channels": d["max_input_channels"],
                    "sample_rate": int(d["default_samplerate"]),
                })
        return {"devices": devices, "has_blackhole": has_blackhole, "model": self.whisper_model}

    async def start_session(
        self,
        *,
        account: str,
        opp_slug: str | None,
        mic_device: int,
        call_device: int | None = None,
        mic_label: str | None = None,
        call_label: str | None = None,
        opportunity: str | None = None,
    ) -> dict[str, Any]:
        account = self.safe_name(account)
        try:
            sess = LiveSession(
                account=account,
                opp_slug=opp_slug,
                mic_device=mic_device,
                call_device=call_device,
                mic_label=mic_label or "You",
                call_label=call_label or "Call",
                opportunity=opportunity,
                persist_fn=self._save_session,
                get_whisper=self._get_whisper,
            )
            sess.start()
        except Exception as e:  # noqa: BLE001
            raise TranscriptionError(500, security.redact_sensitive(f"Could not start capture: {e}")) from e

        sid = uuid.uuid4().hex[:12]
        sess.session_id = sid
        self.sessions[sid] = sess
        ok = await asyncio.to_thread(self._save_session, sess.to_dict())
        if not ok:
            sess.persistence_warning = "Live transcript will not survive a server restart because state could not be saved."

        result: dict[str, Any] = {
            "session_id": sid,
            "labeled": sess.labeled,
            "mic_label": sess.mic_label,
            "call_label": sess.call_label,
        }
        if sess.persistence_warning:
            result["persistence_warning"] = sess.persistence_warning
        return result

    def active_session(self, account: str, opp_slug: str | None = None) -> dict[str, Any] | None:
        account = self.safe_name(account)
        for sid, sess in self.sessions.items():
            if sess.account == account and sess.opp_slug == opp_slug:
                result: dict[str, Any] = {
                    "session_id": sid,
                    "labeled": sess.labeled,
                    "started_at": sess.started_at.timestamp(),
                    "segments": list(sess.segments),
                    "mic_label": sess.mic_label,
                    "call_label": sess.call_label,
                    "recovered": sess.recovered,
                }
                if sess.persistence_warning:
                    result["persistence_warning"] = sess.persistence_warning
                return result
        return None

    async def stream_session(self, session_id: str):
        sess = self.sessions.get(session_id)
        if not sess:
            raise TranscriptionError(404, "Unknown session")

        # replay any segments already captured (e.g. reconnect)
        for seg in list(sess.segments):
            yield {"event": "segment", "data": json.dumps(seg)}
        if sess.recovered:
            # Audio capture is gone after a restart; don't hold the connection open.
            return
        while session_id in self.sessions:
            try:
                seg = await asyncio.wait_for(sess.queue.get(), timeout=15)
                yield {"event": "segment", "data": json.dumps(seg)}
            except asyncio.TimeoutError:
                yield {"event": "ping", "data": "{}"}  # keep-alive

    def stop_session(self, session_id: str) -> dict[str, Any]:
        sess = self.sessions.pop(session_id, None)
        if not sess:
            raise TranscriptionError(404, "Unknown session")
        sess.stop()
        text = sess.transcript_text()
        # Save to _transcripts/<Customer>-MM.DD.YY.txt (the convention post-call consumes).
        transcripts_dir = self.customers_dir / "_transcripts"
        transcripts_dir.mkdir(parents=True, exist_ok=True)
        cust = self.titlecase(sess.account)
        datestr = sess.started_at.astimezone().strftime("%m.%d.%y")
        base = f"{cust}-{datestr}"
        path = transcripts_dir / f"{base}.txt"
        n = 2
        while path.exists():
            path = transcripts_dir / f"{base}-v{n}.txt"
            n += 1
        path.write_text(text)
        delete_ok = persistence.delete_session(session_id, self.workspace)
        result: dict[str, Any] = {
            "saved_to": str(path),
            "segments": len(sess.segments),
            "chars": len(text),
            "transcript": text,
        }
        if not delete_ok:
            result["persistence_warning"] = (
                "The saved transcript was written, but the session state file could not be removed; "
                "it may reappear on restart."
            )
        return result

    # ------------------------------------------------------------------
    # Saved transcripts
    # ------------------------------------------------------------------
    def list_transcripts(self, account: str) -> dict[str, Any]:
        """List saved transcripts for this account, newest first (by filename date
        then mtime). Powers the 'Past transcripts' list on the transcribe page."""
        cust = self.titlecase(self.safe_name(account))
        tdir = self.customers_dir / "_transcripts"
        if not tdir.exists():
            return {"transcripts": []}
        items = []
        for p in tdir.glob(f"{cust}-*.txt"):
            try:
                items.append({"name": p.name, "mtime": p.stat().st_mtime, "size": p.stat().st_size})
            except OSError:
                pass
        items.sort(key=lambda x: (x["name"], x["mtime"]), reverse=True)
        return {"transcripts": items}

    def _transcript_path(self, account: str, name: str) -> Path:
        """Resolve a saved-transcript filename safely under _transcripts/, scoped to
        the account so one opp can't load another's files."""
        name = self.safe_name(name)
        if not name.endswith(".txt"):
            raise TranscriptionError(400, "Not a transcript file")
        cust = self.titlecase(self.safe_name(account))
        if not name.startswith(cust + "-"):
            raise TranscriptionError(403, "Transcript does not belong to this account")
        try:
            path = resolve_within(self.customers_dir / "_transcripts", name)
        except ValueError as exc:
            raise TranscriptionError(400, "Invalid path") from exc
        return path

    def load_transcript(self, account: str, name: str) -> dict[str, Any]:
        """Load one saved transcript as segments + raw text so the page can render
        it read-only and the copilot can answer questions about it."""
        path = self._transcript_path(account, name)
        if not path.exists():
            raise TranscriptionError(404, "Transcript not found")
        text = path.read_text()
        parsed = _parse_saved_transcript(text)
        return {
            "name": name,
            "segments": parsed["segments"],
            "transcript": text,
            "mic_label": parsed["mic_label"],
            "call_label": parsed["call_label"],
        }

    def ask_context(
        self,
        *,
        session_id: str,
        account: str | None = None,
        transcript_name: str | None = None,
        opportunity: str | None = None,
    ) -> tuple[str, str, str | None, bool]:
        """Return (transcript_text, account, opportunity, live) for an Ask request.

        `session_id == "file"` means the page reopened a saved transcript; the file
        is resolved from `account`/`transcript_name`. Otherwise the live session
        registry is used.
        """
        if session_id == "file":
            if not transcript_name or not account:
                raise TranscriptionError(400, "File ask requires transcript_name + account")
            path = self._transcript_path(account, transcript_name)
            if not path.exists():
                raise TranscriptionError(404, "Transcript not found")
            return path.read_text(), self.safe_name(account), opportunity, False

        sess = self.sessions.get(session_id)
        if not sess:
            raise TranscriptionError(404, "Unknown session")
        return sess.transcript_text(), sess.account, sess.opportunity, True
