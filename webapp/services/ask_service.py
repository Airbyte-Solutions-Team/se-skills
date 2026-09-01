"""Ask (Q&A) service for the SE Skills webapp.

Handles the quick (Anthropic API streaming) and deep (claude -p job) paths for
asking follow-up questions against a generated output. It does not own job
lifecycle, output filesystem traversal, or account discovery.
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import AsyncIterable, Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

import md_render
import security
from services.job_service import JobService
from services.output_service import OutputError, OutputService

logger = logging.getLogger(__name__)

# Heuristic: questions that need the codebase / a skill go to claude -p.
DEEP_HINTS = (
    "codebase", "connector", "feasib", "troubleshoot", "schema", "api ",
    "rate limit", "cdc", "deployment", "self-managed", "repo", "error",
    "poc", "meddpicc", "qualif", "edge case",
)

# Quick-path self-escalation sentinel: the model is instructed to emit this
# exact line when it can't fully answer from the document/transcript alone.
_NEEDS_DEEP_RE = re.compile(r"^[ \t]*NEEDS_DEEP:\s*(.+?)[ \t]*$", re.IGNORECASE | re.MULTILINE)


class AskError(Exception):
    """Domain exception carrying an HTTP-like status code and detail."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


_KEYRING_SERVICE = "se-skills"
_GLOBAL_USERNAME = "ANTHROPIC_API_KEY"


def _keyring_username(member_id: str | None) -> str:
    return f"anthropic_api_key:{member_id}" if member_id else _GLOBAL_USERNAME


def _keyring_get(username: str) -> str | None:
    """Best-effort read. Any backend error is treated as "no key" so the app
    degrades to the deep `claude -p` path rather than failing."""
    try:
        import keyring
        import keyring.errors

        return keyring.get_password(_KEYRING_SERVICE, username)
    except (ImportError, keyring.errors.KeyringError, RuntimeError, OSError):
        return None
    except Exception:  # noqa: BLE001
        return None


def _keyring_set(username: str, value: str) -> None:
    """Unlike `_keyring_get`, a write failure is NOT swallowed — a fake "Saved!"
    that didn't actually persist is a worse, more confusing failure than a
    visible error telling the SE their machine has no keyring backend."""
    try:
        import keyring
        import keyring.errors

        keyring.set_password(_KEYRING_SERVICE, username, value)
    except (ImportError, keyring.errors.KeyringError, RuntimeError, OSError) as e:
        raise AskError(503, "No OS keyring backend available; could not save key") from e


def _keyring_delete(username: str) -> None:
    """Deleting an already-absent entry is not an error; other backend
    failures surface loudly, same rationale as `_keyring_set`."""
    try:
        import keyring
        import keyring.errors

        keyring.delete_password(_KEYRING_SERVICE, username)
    except keyring.errors.PasswordDeleteError:
        pass
    except (ImportError, keyring.errors.KeyringError, RuntimeError, OSError) as e:
        raise AskError(503, "No OS keyring backend available; could not remove key") from e


def member_api_key_configured(member_id: str) -> bool:
    """Does this member have their OWN key saved? (ignores the env var and the
    legacy unscoped keyring entry — backs the Settings-tab status pill.)"""
    return bool(_keyring_get(_keyring_username(member_id)))


def save_member_api_key(member_id: str, api_key: str) -> None:
    key = (api_key or "").strip()
    if not key:
        raise AskError(400, "Empty API key")
    _keyring_set(_keyring_username(member_id), key)


def clear_member_api_key(member_id: str) -> None:
    _keyring_delete(_keyring_username(member_id))


def anthropic_api_key(member_id: str | None = None) -> str | None:
    """Return the Anthropic API key for the quick ask-bar path.

    Priority: `ANTHROPIC_API_KEY` environment variable (global override) ->
    this member's keyring entry -> the legacy unscoped keyring entry (kept so
    anyone who already set a personal global key doesn't lose it) -> None.
    No plaintext `~/.mcp/*.env` files are read.
    """
    env = os.environ.get("ANTHROPIC_API_KEY")
    if env:
        return env
    if member_id:
        per_member = _keyring_get(_keyring_username(member_id))
        if per_member:
            return per_member
    return _keyring_get(_GLOBAL_USERNAME)


@dataclass
class AskResult:
    """Discriminated result from an Ask request."""

    kind: Literal["quick", "deep", "needs_deep"]
    stream: AsyncIterable[dict] | None = None
    job_id: str | None = None
    persistence_warning: str | None = None
    reason: str | None = None


class AskService:
    """Cohesive Ask behavior for output-specific follow-up questions."""

    def __init__(
        self,
        *,
        output_service: OutputService,
        job_service: JobService,
        api_key: Callable[[str | None], str | None] = anthropic_api_key,
        model_for: Callable[[str], str],
        render_markdown: Callable[[str], str] | None = None,
        redact: Callable[[str], str] | None = None,
        deep_hints: tuple[str, ...] = DEEP_HINTS,
        output_tail: int = 16_000,
        quick_max_tokens: int = 800,
    ) -> None:
        self.output_service = output_service
        self.job_service = job_service
        self.api_key = api_key
        self.model_for = model_for
        self.render_markdown = render_markdown or md_render.markdown_to_body_html
        self.redact = redact or security.redact_sensitive
        self.deep_hints = deep_hints
        self.output_tail = output_tail
        self.quick_max_tokens = quick_max_tokens

    def _is_deep(self, question: str) -> bool:
        q = question.lower()
        return any(h in q for h in self.deep_hints)

    def _prior_answer_block(self, prior_answer: str | None) -> str:
        """Shared "a quick pass already tried this" block for a deep prompt.

        Redacted and hard-truncated because `prior_answer` is client-supplied
        input on a new code path (unlike `context`, which is server-read file/
        transcript content on an already-shipped path).
        """
        if not prior_answer:
            return ""
        trimmed = self.redact(prior_answer)[:4000]
        return (
            f"\nA quick pass already gave this partial/possibly-incomplete answer:\n"
            f"--- PRIOR ANSWER ---\n{trimmed}\n--- END PRIOR ANSWER ---\n"
            f"The SE flagged this as insufficient or incomplete. Verify it, correct any "
            f"inaccuracies, and fill the gaps using the codebase/skills — don't just restate it.\n"
        )

    def _build_deep_prompt(
        self,
        context: str,
        question: str,
        account: str | None,
        opportunity: str | None,
        *,
        source_label: str,
        context_label: str,
        prior_answer: str | None = None,
    ) -> str:
        acct = account or ""
        preamble = (
            f"A Solutions Engineer is reviewing this {source_label}"
            f"{(' for the account ' + repr(acct)) if acct else ''}"
            f"{(', opportunity ' + repr(opportunity)) if opportunity else ''} and has a follow-up question.\n\n"
        )
        return (
            f"{preamble}"
            f"=== {context_label} ===\n{context}\n=== END {context_label} ===\n\n"
            f"{self._prior_answer_block(prior_answer)}"
            f"Follow-up question: {question}\n\n"
            f"Answer concisely and practically. If it involves Airbyte connectors, deployment, or the "
            f"codebase, use the relevant SE skills / inspect the repo as needed."
        )

    def _quick_stream(
        self,
        *,
        use: str,
        max_tokens: int,
        system: str,
        content: str,
        on_needs_deep: Callable[[str, str], Awaitable[dict]] | None = None,
        member_id: str | None = None,
    ) -> AsyncIterable[dict]:
        """Stream a quick answer over SSE for the configured use/model.

        If the model ends its answer with the `NEEDS_DEEP:` sentinel and
        `on_needs_deep` is provided, the callback is awaited (it launches the
        deep job) and an `escalate` event is yielded before the final `done` —
        `done` always stays last so callers can rely on stream-end signaling.
        """
        model = self.model_for(use)

        async def gen() -> AsyncIterable[dict]:
            try:
                from anthropic import AsyncAnthropic

                client = AsyncAnthropic(api_key=self.api_key(member_id))
                acc = ""
                async with client.messages.stream(
                    model=model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": content}],
                ) as stream:
                    async for text in stream.text_stream:
                        acc += text
                        html = self.render_markdown(acc)
                        yield {"event": "token", "data": json.dumps({"text": text, "html": html})}

                match = _NEEDS_DEEP_RE.search(acc)
                reason: str | None = None
                clean = acc
                if match:
                    reason = match.group(1).strip()
                    clean = _NEEDS_DEEP_RE.sub("", acc, count=1).strip()
                elif not acc.strip():
                    # The model returned a genuinely empty completion (observed
                    # intermittently — not tied to a specific document or
                    # question). Auto-escalate instead of leaving a dead end;
                    # the SE should never see "no response" when a working
                    # fallback exists.
                    reason = "the quick path returned an empty response"

                if reason and on_needs_deep:
                    try:
                        extra = await on_needs_deep(reason, clean)
                    except Exception as e:  # noqa: BLE001 — escalation wiring must never kill the stream
                        logger.exception("Auto-escalation failed")
                        extra = {"job_id": None, "persistence_warning": None, "error": self.redact(str(e))}
                    yield {
                        "event": "escalate",
                        "data": json.dumps({"reason": reason, "html": self.render_markdown(clean), **extra}),
                    }
                yield {"event": "done", "data": "{}"}
            except Exception as e:  # noqa: BLE001
                logger.exception("Quick ask streaming failed")
                yield {"event": "error", "data": json.dumps({"error": self.redact(str(e))})}

        return gen()

    async def output_ask(
        self,
        *,
        path: str,
        question: str,
        account: str | None = None,
        opportunity: str | None = None,
        force_deep: bool = False,
        prior_answer: str | None = None,
        member_id: str | None = None,
    ) -> AskResult:
        """Answer a follow-up question about a generated output.

        Returns a quick SSE stream (which may self-escalate mid-stream via an
        `escalate` event) or a deep job reference.
        """
        q = (question or "").strip()
        if not q:
            raise AskError(400, "Empty question")

        try:
            doc = self.output_service.read_output_content(path)
        except OutputError as e:
            if e.status_code == 404:
                raise AskError(404, "Not found") from e
            raise AskError(e.status_code, e.detail) from e

        context = doc[-self.output_tail:]

        async def launch_deep(prior: str | None, *, escalation: str | None) -> tuple[str, str | None]:
            prompt = self._build_deep_prompt(
                context,
                q,
                account,
                opportunity,
                source_label="generated document",
                context_label="DOCUMENT",
                prior_answer=prior,
            )
            sig = ("output-ask", path, q[:60], "auto") if escalation == "auto" else ("output-ask", path, q[:60])
            return await self.job_service.launch(
                account=account or "?",
                opp_slug=None,
                skill="output-ask",
                opportunity=opportunity,
                sig=sig,
                prompt=prompt,
                meta={
                    "account": account or "?",
                    "opp_slug": None,
                    "skill": "output-ask",
                    "opportunity": opportunity,
                    "escalation": escalation,
                },
            )

        # No API key means the quick path can never work, regardless of what
        # the question is about — route to claude -p unconditionally instead
        # of keyword-gating first (a "re-ask" would hit the identical
        # keyword miss and loop forever).
        if force_deep or self._is_deep(q) or not self.api_key(member_id):
            job_id, persist_warn = await launch_deep(
                prior_answer, escalation="manual" if force_deep else None
            )
            return AskResult(kind="deep", job_id=job_id, persistence_warning=persist_warn)

        async def on_needs_deep(reason: str, partial: str) -> dict:
            job_id, persist_warn = await launch_deep(partial, escalation="auto")
            return {"job_id": job_id, "persistence_warning": persist_warn}

        return AskResult(
            kind="quick",
            stream=self._quick_stream(
                use="quick-ask",
                max_tokens=self.quick_max_tokens,
                system=(
                    "You are a Solutions Engineer's copilot. Answer the follow-up briefly and directly "
                    "from the document provided. If — and only if — you cannot adequately answer from "
                    "the document alone, end your reply with a final line, exactly: "
                    "`NEEDS_DEEP: <short reason>`. Do not include this line if you were able to answer."
                ),
                content=f"Document:\n\n{context}\n\nFollow-up question: {q}",
                on_needs_deep=on_needs_deep,
                member_id=member_id,
            ),
        )

    async def transcript_ask(
        self,
        *,
        transcript: str,
        question: str,
        account: str | None,
        opportunity: str | None,
        live: bool,
        session_id: str,
        force_deep: bool = False,
        prior_answer: str | None = None,
        member_id: str | None = None,
    ) -> AskResult:
        """Answer a follow-up question about a live or saved call transcript."""
        q = (question or "").strip()
        if not q:
            raise AskError(400, "Empty question")

        context = transcript[-12000:] if live else transcript[-60000:]
        when = "LIVE during a customer call" if live else "reviewing a saved call transcript"
        tlabel = "live call transcript so far" if live else "full saved call transcript"

        async def launch_deep(prior: str | None, *, escalation: str | None) -> tuple[str, str | None]:
            prompt = (
                f"You are assisting a Solutions Engineer {when} for the account "
                f"'{account or '?'}'{(', opportunity ' + repr(opportunity)) if opportunity else ''}. "
                f"Here is the {tlabel}:\n\n{context}\n\n"
                f"{self._prior_answer_block(prior)}"
                f"The SE asks: {q}\n\n"
                f"Answer concisely and practically. If it involves Airbyte connectors, "
                f"deployment, or the codebase, use the relevant SE skills / inspect the repo as needed."
            )
            sig = ("live", session_id, q[:60], "auto") if escalation == "auto" else ("live", session_id, q[:60])
            return await self.job_service.launch(
                account=account or "?",
                opp_slug=None,
                skill="live-ask",
                opportunity=opportunity,
                sig=sig,
                prompt=prompt,
                meta={
                    "account": account or "?",
                    "opp_slug": None,
                    "skill": "live-ask",
                    "opportunity": opportunity,
                    "escalation": escalation,
                },
            )

        # See output_ask: no API key means the quick path can never work, so
        # route to claude -p unconditionally rather than keyword-gating first.
        if force_deep or self._is_deep(q) or not self.api_key(member_id):
            job_id, persist_warn = await launch_deep(
                prior_answer, escalation="manual" if force_deep else None
            )
            return AskResult(kind="deep", job_id=job_id, persistence_warning=persist_warn)

        async def on_needs_deep(reason: str, partial: str) -> dict:
            job_id, persist_warn = await launch_deep(partial, escalation="auto")
            return {"job_id": job_id, "persistence_warning": persist_warn}

        system = (
            "You are a Solutions Engineer's live call copilot. Answer briefly and "
            "directly from the call transcript provided. If — and only if — you cannot "
            "adequately answer from the transcript alone, end your reply with a final "
            "line, exactly: `NEEDS_DEEP: <short reason>`. Do not include this line if "
            "you were able to answer."
        )
        return AskResult(
            kind="quick",
            stream=self._quick_stream(
                use="live-ask",
                max_tokens=700,
                system=system,
                content=f"Transcript:\n\n{context}\n\nQuestion: {q}",
                on_needs_deep=on_needs_deep,
                member_id=member_id,
            ),
        )

    def ai_status(self, member_id: str | None = None) -> bool:
        """Return whether the fast quick-ask path is available."""
        return bool(self.api_key(member_id))
