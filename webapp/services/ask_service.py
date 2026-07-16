"""Ask (Q&A) service for the SE Skills webapp.

Handles the quick (Anthropic API streaming) and deep (claude -p job) paths for
asking follow-up questions against a generated output. It does not own job
lifecycle, output filesystem traversal, or account discovery.
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterable, Callable
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


class AskError(Exception):
    """Domain exception carrying an HTTP-like status code and detail."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _anthropic_key_from_keyring() -> str | None:
    """Best-effort: read ANTHROPIC_API_KEY from the OS keyring via the
    `keyring` module. Any backend error is treated as "no key" so the app
    degrades to the deep `claude -p` path.
    """
    try:
        import keyring
        import keyring.errors

        return keyring.get_password("se-skills", "ANTHROPIC_API_KEY")
    except (ImportError, keyring.errors.KeyringError, RuntimeError, OSError):
        return None
    except Exception:  # noqa: BLE001
        return None


def anthropic_api_key() -> str | None:
    """Return the Anthropic API key for the quick ask-bar path.

    Priority: `ANTHROPIC_API_KEY` environment variable, then the OS keyring.
    No plaintext `~/.mcp/*.env` files are read.
    """
    return os.environ.get("ANTHROPIC_API_KEY") or _anthropic_key_from_keyring()


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
        api_key: Callable[[], str | None] = anthropic_api_key,
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

    def _build_deep_prompt(
        self,
        context: str,
        question: str,
        account: str | None,
        opportunity: str | None,
        *,
        source_label: str,
        context_label: str,
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
    ) -> AsyncIterable[dict]:
        """Stream a quick answer over SSE for the configured use/model."""
        model = self.model_for(use)

        async def gen() -> AsyncIterable[dict]:
            try:
                from anthropic import AsyncAnthropic

                client = AsyncAnthropic(api_key=self.api_key())
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
    ) -> AskResult:
        """Answer a follow-up question about a generated output.

        Returns a quick SSE stream, a deep job reference, or a `needs_deep`
        fallback if no Anthropic key is available.
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

        if self._is_deep(q):
            prompt = self._build_deep_prompt(
                context,
                q,
                account,
                opportunity,
                source_label="generated document",
                context_label="DOCUMENT",
            )
            job_id, persist_warn = await self.job_service.launch(
                account=account or "?",
                opp_slug=None,
                skill="output-ask",
                opportunity=opportunity,
                sig=("output-ask", path, q[:60]),
                prompt=prompt,
                meta={
                    "account": account or "?",
                    "opp_slug": None,
                    "skill": "output-ask",
                    "opportunity": opportunity,
                },
            )
            return AskResult(kind="deep", job_id=job_id, persistence_warning=persist_warn)

        if not self.api_key():
            return AskResult(
                kind="needs_deep",
                reason="No ANTHROPIC_API_KEY for the quick path — re-ask routes to claude -p.",
            )

        return AskResult(
            kind="quick",
            stream=self._quick_stream(
                use="quick-ask",
                max_tokens=self.quick_max_tokens,
                system=(
                    "You are a Solutions Engineer's copilot. Answer the follow-up briefly and directly "
                    "from the document provided. If the question needs the Airbyte codebase or a deep "
                    "skill, say so in one line."
                ),
                content=f"Document:\n\n{context}\n\nFollow-up question: {q}",
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
    ) -> AskResult:
        """Answer a follow-up question about a live or saved call transcript."""
        q = (question or "").strip()
        if not q:
            raise AskError(400, "Empty question")

        context = transcript[-12000:] if live else transcript[-60000:]

        if self._is_deep(q):
            when = "LIVE during a customer call" if live else "reviewing a saved call transcript"
            tlabel = "live call transcript so far" if live else "full saved call transcript"
            prompt = (
                f"You are assisting a Solutions Engineer {when} for the account "
                f"'{account or '?'}'{(', opportunity ' + repr(opportunity)) if opportunity else ''}. "
                f"Here is the {tlabel}:\n\n{context}\n\n"
                f"The SE asks: {q}\n\n"
                f"Answer concisely and practically. If it involves Airbyte connectors, "
                f"deployment, or the codebase, use the relevant SE skills / inspect the repo as needed."
            )
            job_id, persist_warn = await self.job_service.launch(
                account=account or "?",
                opp_slug=None,
                skill="live-ask",
                opportunity=opportunity,
                sig=("live", session_id, q[:60]),
                prompt=prompt,
                meta={
                    "account": account or "?",
                    "opp_slug": None,
                    "skill": "live-ask",
                    "opportunity": opportunity,
                },
            )
            return AskResult(kind="deep", job_id=job_id, persistence_warning=persist_warn)

        if not self.api_key():
            return AskResult(
                kind="needs_deep",
                reason="No ANTHROPIC_API_KEY for the quick path — re-ask routes to claude -p.",
            )

        system = (
            "You are a Solutions Engineer's live call copilot. Answer briefly and "
            "directly from the call transcript provided. If the question needs the "
            "Airbyte codebase or a deep skill, say so in one line."
        )
        return AskResult(
            kind="quick",
            stream=self._quick_stream(
                use="live-ask",
                max_tokens=700,
                system=system,
                content=f"Transcript:\n\n{context}\n\nQuestion: {q}",
            ),
        )

    def ai_status(self) -> bool:
        """Return whether the fast quick-ask path is available."""
        return bool(self.api_key())
