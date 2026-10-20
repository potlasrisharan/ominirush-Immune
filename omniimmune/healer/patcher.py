from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

import libcst as cst

from omniimmune.models import BugClass, PatchResult


def _name(value: str) -> cst.Name:
    return cst.Name(value)


def _attribute(value: str, attr: str) -> cst.Attribute:
    return cst.Attribute(value=_name(value), attr=_name(attr))


def _json_response_import() -> cst.SimpleStatementLine:
    return cst.parse_statement("from fastapi.responses import JSONResponse\n")  # type: ignore[return-value]


def _asyncio_import() -> cst.SimpleStatementLine:
    return cst.parse_statement("import asyncio\n")  # type: ignore[return-value]


def _qualified_name(node: cst.CSTNode | None) -> str:
    if isinstance(node, cst.Name):
        return node.value
    if isinstance(node, cst.Attribute):
        left = _qualified_name(node.value)
        return f"{left}.{node.attr.value}" if left else node.attr.value
    return ""


def _is_import(statement: cst.BaseStatement, module: str) -> bool:
    if not isinstance(statement, cst.SimpleStatementLine):
        return False
    for small in statement.body:
        if isinstance(small, cst.Import):
            for alias in small.names:
                if _qualified_name(alias.name) == module:
                    return True
        if isinstance(small, cst.ImportFrom):
            if _qualified_name(small.module) == module:
                return True
    return False


def _contains_import(module: cst.Module, required: cst.SimpleStatementLine) -> bool:
    required_small = required.body[0]
    for statement in module.body:
        if not isinstance(statement, cst.SimpleStatementLine):
            continue
        for small in statement.body:
            if isinstance(required_small, cst.Import) and isinstance(small, cst.Import):
                required_names = {_qualified_name(alias.name) for alias in required_small.names}
                existing_names = {_qualified_name(alias.name) for alias in small.names}
                if required_names <= existing_names:
                    return True
            elif isinstance(required_small, cst.ImportFrom) and isinstance(small, cst.ImportFrom):
                if _qualified_name(required_small.module) != _qualified_name(small.module):
                    continue
                required_names = {
                    _qualified_name(alias.name) for alias in required_small.names
                    if isinstance(alias, cst.ImportAlias)
                }
                existing_names = {
                    _qualified_name(alias.name) for alias in small.names
                    if isinstance(alias, cst.ImportAlias)
                }
                if required_names <= existing_names:
                    return True
    return False


def _ensure_import(module: cst.Module, statement: cst.SimpleStatementLine) -> cst.Module:
    imported_module = ""
    for small in statement.body:
        if isinstance(small, cst.Import):
            imported_module = next(
                (
                    _qualified_name(alias.name)
                    for alias in small.names
                ),
                "",
            )
        elif isinstance(small, cst.ImportFrom):
            imported_module = _qualified_name(small.module)
    if _contains_import(module, statement):
        return module

    insert_at = 0
    for index, existing in enumerate(module.body):
        if (
            index == 0
            and isinstance(existing, cst.SimpleStatementLine)
            and len(existing.body) == 1
            and isinstance(existing.body[0], cst.Expr)
            and isinstance(existing.body[0].value, cst.SimpleString)
        ):
            # Keep a module docstring first; moving it below an injected
            # import changes ``__doc__`` and can break runtime metadata.
            insert_at = 1
        if _is_import(existing, "__future__") or _is_import(existing, "fastapi") or _is_import(
            existing, "httpx"
        ) or _is_import(existing, "threading") or _is_import(existing, "time"):
            insert_at = index + 1
    body = [*module.body]
    body.insert(insert_at, statement)
    return module.with_changes(body=body)


def _error_response(message: str, status_code: int) -> cst.SimpleStatementLine:
    return cst.parse_statement(
        f'return JSONResponse(status_code={status_code}, content={{"error": {message!r}, "status": "rejected"}})\n'
    )  # type: ignore[return-value]


class PanicGuardTransformer(cst.CSTTransformer):
    def leave_FunctionDef(
        self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef
    ) -> cst.FunctionDef:
        if original_node.name.value != "create_order":
            return updated_node
        parameter_names = {
            parameter.name.value
            for parameter in original_node.params.params
        }
        quantity_expression = "quantity" if "quantity" in parameter_names else "order.quantity"
        guard = cst.parse_module(
            f'if {quantity_expression} <= 0:\n    return JSONResponse(status_code=400, content={{"error": "quantity must be greater than zero", "status": "rejected"}})\n'
        ).body
        if isinstance(updated_node.body, cst.IndentedBlock):
            return updated_node.with_changes(
                body=updated_node.body.with_changes(body=[*guard, *updated_node.body.body])
            )
        return updated_node


class PanicBoundaryTransformer(cst.CSTTransformer):
    def leave_FunctionDef(
        self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef
    ) -> cst.FunctionDef:
        if original_node.name.value != "create_order" or not isinstance(
            updated_node.body, cst.IndentedBlock
        ):
            return updated_node
        handler = cst.ExceptHandler(
            type=cst.Tuple(
                elements=[
                    cst.Element(_name("ValueError")),
                    cst.Element(_name("ZeroDivisionError")),
                ]
            ),
            body=cst.IndentedBlock(
                body=[
                    cst.parse_statement(
                        'return JSONResponse(status_code=400, content={"error": "order rejected", "status": "rejected"})\n'
                    )
                ]
            ),
        )
        wrapped = cst.Try(
            body=cst.IndentedBlock(body=list(updated_node.body.body)),
            handlers=[handler],
        )
        return updated_node.with_changes(body=cst.IndentedBlock(body=[wrapped]))


class AsyncLockTransformer(cst.CSTTransformer):
    def leave_Assign(
        self, original_node: cst.Assign, updated_node: cst.Assign
    ) -> cst.Assign:
        if len(original_node.targets) != 1:
            return updated_node
        target = original_node.targets[0].target
        value = original_node.value
        if (
            isinstance(target, cst.Name)
            and target.value == "_inventory_lock"
            and isinstance(value, cst.Call)
            and isinstance(value.func, cst.Attribute)
            and isinstance(value.func.value, cst.Name)
            and value.func.value.value == "threading"
            and value.func.attr.value == "Lock"
        ):
            return updated_node.with_changes(value=cst.Call(func=_attribute("asyncio", "Lock")))
        return updated_node

    def leave_With(
        self, original_node: cst.With, updated_node: cst.With
    ) -> cst.With:
        if any(
            isinstance(item.item, cst.Name) and item.item.value == "_inventory_lock"
            for item in original_node.items
        ):
            return updated_node.with_changes(asynchronous=cst.Asynchronous())
        return updated_node

    def leave_Call(self, original_node: cst.Call, updated_node: cst.Call) -> cst.CSTNode:
        if (
            isinstance(original_node.func, cst.Attribute)
            and isinstance(original_node.func.value, cst.Name)
            and original_node.func.value.value == "time"
            and original_node.func.attr.value == "sleep"
        ):
            return cst.Await(
                expression=updated_node.with_changes(
                    func=_attribute("asyncio", "sleep"),
                    args=[cst.Arg(cst.Float("0.01"))],
                )
            )
        return updated_node


class ThreadPoolTransformer(cst.CSTTransformer):
    """Remove the blocking critical section and offload its sleep to a thread."""

    def leave_With(
        self, original_node: cst.With, updated_node: cst.With
    ) -> cst.CSTNode:
        if any(
            isinstance(item.item, cst.Name) and item.item.value == "_inventory_lock"
            for item in original_node.items
        ) and isinstance(updated_node.body, cst.IndentedBlock):
            return cst.FlattenSentinel(updated_node.body.body)
        return updated_node

    def leave_Call(self, original_node: cst.Call, updated_node: cst.Call) -> cst.CSTNode:
        if (
            isinstance(original_node.func, cst.Attribute)
            and isinstance(original_node.func.value, cst.Name)
            and original_node.func.value.value == "time"
            and original_node.func.attr.value == "sleep"
        ):
            return cst.Await(
                expression=cst.Call(
                    func=_attribute("asyncio", "to_thread"),
                    args=[
                        cst.Arg(updated_node.func),
                        cst.Arg(cst.Float("0.01")),
                    ],
                )
            )
        return updated_node


class TimeoutTransformer(cst.CSTTransformer):
    def __init__(self, add_boundary: bool) -> None:
        self.add_boundary = add_boundary

    def leave_Arg(self, original_node: cst.Arg, updated_node: cst.Arg) -> cst.Arg:
        if (
            original_node.keyword is not None
            and original_node.keyword.value == "timeout"
            and isinstance(original_node.value, cst.Name)
            and original_node.value.value == "None"
        ):
            return updated_node.with_changes(value=cst.Float("1.5"))
        return updated_node

    def leave_FunctionDef(
        self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef
    ) -> cst.FunctionDef:
        if (
            not self.add_boundary
            or original_node.name.value != "verify_payment"
            or not isinstance(updated_node.body, cst.IndentedBlock)
        ):
            return updated_node
        handler = cst.ExceptHandler(
            type=cst.Tuple(
                elements=[
                    cst.Element(_attribute("httpx", "TimeoutException")),
                    cst.Element(_attribute("httpx", "RequestError")),
                ]
            ),
            body=cst.IndentedBlock(
                body=[
                    cst.parse_statement(
                        'return JSONResponse(status_code=504, content={"verified": False, "error": "upstream_timeout_handled"})\n'
                    )
                ]
            ),
        )
        wrapped = cst.Try(
            body=cst.IndentedBlock(body=list(updated_node.body.body)),
            handlers=[handler],
        )
        return updated_node.with_changes(body=cst.IndentedBlock(body=[wrapped]))


class FallbackTransformer(cst.CSTTransformer):
    def leave_FunctionDef(
        self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef
    ) -> cst.FunctionDef:
        if original_node.name.value != "verify_payment" or not isinstance(
            updated_node.body, cst.IndentedBlock
        ):
            return updated_node
        fallback = cst.parse_module(
            'return JSONResponse(status_code=503, content={"verified": False, "error": "upstream_unavailable"})\n'
        ).body[0]
        return updated_node.with_changes(body=cst.IndentedBlock(body=[fallback, *updated_node.body.body]))


class BoundedCacheTransformer(cst.CSTTransformer):
    """Turn simple dictionary caches into bounded, eviction-aware caches.

    The transformer deliberately only touches names containing ``cache``.  It
    therefore remains safe for arbitrary target applications while covering
    the common ``cache = {}; cache[key] = value`` resource exhaustion bug.
    """

    def __init__(self, max_entries: int = 1000) -> None:
        self.max_entries = max_entries
        self.cache_names: set[str] = set()
        self.deque_names: set[str] = set()

    @staticmethod
    def _cache_name(target: cst.BaseExpression) -> str | None:
        if isinstance(target, cst.Name) and "cache" in target.value.lower():
            return target.value
        return None

    def leave_Assign(self, original_node: cst.Assign, updated_node: cst.Assign) -> cst.Assign:
        if len(original_node.targets) != 1:
            return updated_node
        target = original_node.targets[0].target
        name = self._cache_name(target)
        if name is None:
            return updated_node
        if isinstance(original_node.value, cst.Dict):
            self.cache_names.add(name)
            return updated_node.with_changes(
                value=cst.Call(func=_name("OrderedDict"), args=[])
            )
        if isinstance(original_node.value, cst.List):
            self.deque_names.add(name)
            return updated_node.with_changes(
                value=cst.Call(
                    func=_name("deque"),
                    args=[cst.Arg(keyword=cst.Name("maxlen"), value=cst.Integer(str(self.max_entries)))],
                )
            )
        return updated_node

    def leave_AnnAssign(
        self, original_node: cst.AnnAssign, updated_node: cst.AnnAssign
    ) -> cst.AnnAssign:
        name = self._cache_name(original_node.target)
        if name is None or not isinstance(original_node.value, cst.Dict):
            if name is None or not isinstance(original_node.value, cst.List):
                return updated_node
            self.deque_names.add(name)
            return updated_node.with_changes(
                value=cst.Call(
                    func=_name("deque"),
                    args=[cst.Arg(keyword=cst.Name("maxlen"), value=cst.Integer(str(self.max_entries)))],
                )
            )
        self.cache_names.add(name)
        return updated_node.with_changes(value=cst.Call(func=_name("OrderedDict"), args=[]))

    def leave_SimpleStatementLine(
        self,
        original_node: cst.SimpleStatementLine,
        updated_node: cst.SimpleStatementLine,
    ) -> cst.BaseStatement | cst.FlattenSentinel[cst.BaseStatement]:
        # Add eviction immediately after cache[key] = value.  This keeps the
        # policy local and works for both module-level and function caches.
        if len(original_node.body) != 1 or not isinstance(original_node.body[0], cst.Assign):
            return updated_node
        assignment = original_node.body[0]
        if len(assignment.targets) != 1 or not isinstance(assignment.targets[0].target, cst.Subscript):
            return updated_node
        subscript = assignment.targets[0].target
        if not isinstance(subscript.value, cst.Name) or subscript.value.value not in self.cache_names:
            return updated_node
        name = subscript.value.value
        guard = cst.parse_statement(
            f"if len({name}) > {self.max_entries}:\n"
            f"    {name}.popitem(last=False)\n"
        )
        return cst.FlattenSentinel([updated_node, guard])


def _strip_markdown(response: str) -> str:
    """Extract a diff from a fenced markdown response without executing it."""
    value = response.strip()
    fenced = re.fullmatch(r"```(?:diff|patch)?\s*\n(.*?)\n?```", value, re.DOTALL | re.IGNORECASE)
    if fenced:
        return fenced.group(1).strip("\n") + "\n"
    return value + ("\n" if value and not value.endswith("\n") else "")


def _response_text(response: Any) -> str | None:
    if isinstance(response, str):
        return response
    if not isinstance(response, dict):
        return None
    # OpenAI-compatible and Gemini response shapes, plus a useful simple mock
    # shape (``{"diff": "..."}``) for offline tests.
    if isinstance(response.get("diff"), str):
        return response["diff"]
    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return message["content"]
    candidates = response.get("candidates")
    if isinstance(candidates, list) and candidates:
        content = candidates[0].get("content", {}) if isinstance(candidates[0], dict) else {}
        parts = content.get("parts", []) if isinstance(content, dict) else []
        if isinstance(parts, list):
            text = "".join(part.get("text", "") for part in parts if isinstance(part, dict))
            return text or None
    return None


def _apply_unified_diff(source: str, diff: str, target_file: Path) -> str | None:
    """Apply a single-file unified diff in memory, rejecting malformed input."""
    lines = diff.splitlines(keepends=True)
    if len(lines) < 3 or not lines[0].startswith("--- ") or not lines[1].startswith("+++ "):
        return None

    def header_path(line: str, prefix: str) -> str | None:
        if not line.startswith(prefix):
            return None
        return line[len(prefix):].split("\t", 1)[0].strip()

    old_path, new_path = header_path(lines[0], "--- "), header_path(lines[1], "+++ ")
    if not old_path or not new_path or old_path == "/dev/null" or new_path == "/dev/null":
        return None
    expected = {target_file.name, target_file.as_posix()}
    try:
        expected.add(target_file.resolve().relative_to(Path(__file__).resolve().parents[2]).as_posix())
    except ValueError:
        pass
    if old_path.removeprefix("a/") not in expected or new_path.removeprefix("b/") not in expected:
        return None

    source_lines = source.splitlines(keepends=True)
    output: list[str] = []
    source_pos = 0
    index = 2
    hunk_re = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
    while index < len(lines):
        match = hunk_re.match(lines[index].rstrip("\n"))
        if not match:
            return None
        old_start = int(match.group(1))
        old_count = int(match.group(2) or "1")
        if old_start < 1 or old_start - 1 < source_pos or old_start - 1 > len(source_lines):
            return None
        output.extend(source_lines[source_pos : old_start - 1])
        source_pos = old_start - 1
        index += 1
        consumed = 0
        while index < len(lines) and not lines[index].startswith("@@ "):
            line = lines[index]
            if line.startswith("\\ No newline"):
                index += 1
                continue
            if not line:
                return None
            marker, content = line[0], line[1:]
            if marker == " ":
                if source_pos >= len(source_lines) or source_lines[source_pos].rstrip("\r\n") != content.rstrip("\r\n"):
                    return None
                output.append(source_lines[source_pos])
                source_pos += 1
                consumed += 1
            elif marker == "-":
                if source_pos >= len(source_lines) or source_lines[source_pos].rstrip("\r\n") != content.rstrip("\r\n"):
                    return None
                source_pos += 1
                consumed += 1
            elif marker == "+":
                output.append(content)
            else:
                return None
            index += 1
        if consumed != old_count:
            return None
    output.extend(source_lines[source_pos:])
    return "".join(output)


class LLMFallbackPatcher:
    """Optional, strictly validated LLM repair fallback.

    The default transport is intentionally tiny and dependency-free.  Tests and
    embedders can inject ``llm_call``; no provider is contacted without one of
    the supported environment keys.
    """

    def __init__(
        self,
        target_file: Path | str = "target_app/app.py",
        llm_call: Callable[..., Any] | None = None,
        client: Callable[..., Any] | None = None,
        llm: Callable[..., Any] | None = None,
        max_prompt_chars: int = 120_000,
    ) -> None:
        self.target_file = Path(target_file)
        self.llm_call = llm_call or client or llm
        self.max_prompt_chars = max_prompt_chars
        self.last_modified_source: str | None = None

    def _credential(self) -> tuple[str, str] | None:
        for variable, provider in (("OMNI_LLM_KEY", "openai"), ("OPENAI_API_KEY", "openai"), ("GEMINI_API_KEY", "gemini")):
            key = os.getenv(variable, "").strip()
            if key:
                return provider, key
        return None

    @classmethod
    def is_available(cls) -> bool:
        """Return whether an explicitly configured provider credential exists."""
        return any(
            os.getenv(variable, "").strip()
            for variable in ("OMNI_LLM_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY")
        )

    def build_prompt(
        self,
        source: str,
        bug_class: BugClass | str,
        sig_id: str,
        context: dict[str, Any] | None = None,
    ) -> str:
        bug_value = bug_class.value if isinstance(bug_class, BugClass) else str(bug_class)
        payload = {
            "task": "produce a minimal safe repair",
            "bug_class": bug_value,
            "incident_id": sig_id,
            "file": self.target_file.as_posix(),
            "context": context or {},
            "source": source[: self.max_prompt_chars],
            "output_contract": "Return only a single unified diff with --- a/<file> and +++ b/<file> headers. Do not return markdown or explanations.",
        }
        return json.dumps(payload, sort_keys=True, ensure_ascii=False)

    def _default_call(self, prompt: str, provider: str, key: str) -> Any:
        if provider == "gemini":
            url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key=" + key
            body = {"contents": [{"parts": [{"text": prompt}]}]}
        else:
            url = "https://api.openai.com/v1/chat/completions"
            body = {"model": os.getenv("OMNI_LLM_MODEL", "gpt-4o-mini"), "temperature": 0, "messages": [{"role": "user", "content": prompt}]}
        request = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError, urllib.error.URLError):
            return None

    def synthesize_patch(
        self,
        source_code: str,
        culprit_file: str,
        culprit_line: int,
        traceback_str: str,
        dna_payload: Any,
    ) -> str | None:
        """Ask the configured model for one validated unified diff.

        The returned value is the diff text.  The successfully applied source is
        retained in ``last_modified_source`` for the Arbiter's existing
        ``PatchResult`` flow.  No provider is contacted without an explicit
        supported API key.
        """
        credential = self._credential()
        if credential is None:
            return None
        provider, key = credential
        culprit = str(culprit_file)
        source_slice = source_code[: self.max_prompt_chars]
        prompt = json.dumps(
            {
                "system": "Output ONLY a valid unified diff starting with '--- a/' and '+++ b/' that resolves the vulnerability without breaking functionality.",
                "culprit_file": culprit,
                "culprit_line": int(culprit_line),
                "source_slice": source_slice,
                "traceback": str(traceback_str),
                "triggering_input": dna_payload,
            },
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        try:
            if self.llm_call is None:
                response = self._default_call(prompt, provider, key)
            else:
                try:
                    response = self.llm_call(prompt)
                except TypeError:
                    response = self.llm_call(prompt, provider, key)
        except Exception:
            return None
        text = _response_text(response)
        if not text:
            return None
        diff = _strip_markdown(text)
        modified = _apply_unified_diff(source_code, diff, self.target_file)
        if modified is None or modified == source_code:
            return None
        try:
            compile(modified, str(self.target_file), "exec")
        except (SyntaxError, ValueError, TypeError):
            return None
        self.last_modified_source = modified
        return diff

    def apply_patch(
        self,
        bug_class: BugClass | str,
        sig_id: str,
        source: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> PatchResult | None:
        credential = self._credential()
        if credential is None:
            return None
        provider, key = credential
        original = source if source is not None else self.target_file.read_text(encoding="utf-8")
        context = context or {}
        diff = self.synthesize_patch(
            original,
            str(context.get("culprit_file") or self.target_file),
            int(context.get("culprit_line") or 1),
            str(context.get("stack_trace") or ""),
            context.get("dna_payload", context),
        )
        if diff is None or self.last_modified_source is None:
            return None
        modified = self.last_modified_source
        self.last_modified_source = modified
        strategy = "llm_fallback"
        return PatchResult(
            sig_id=sig_id,
            strategy=strategy,
            file_path=str(self.target_file),
            diff=diff,
            applied=True,
            base_file_sha256=hashlib.sha256(original.encode("utf-8")).hexdigest(),
        )

    patch = apply_patch
    propose_patch = apply_patch
    generate_patch = apply_patch


class PatchLadder:
    STRATEGIES = {
        BugClass.PANIC: ["input_guard", "try_catch_boundary"],
        BugClass.STARVATION: ["asyncio_lock_nonblocking", "thread_pool_offload"],
        BugClass.UPSTREAM_TIMEOUT: ["explicit_timeout_guard", "circuit_breaker_fallback"],
    }
    # T3 adds the enum member as part of the resource-exhaustion attack.  The
    # getattr keeps this module importable while that additive API change is
    # being rolled out and is harmless once the member exists.
    _RESOURCE_EXHAUSTION = getattr(BugClass, "RESOURCE_EXHAUSTION", None)
    if _RESOURCE_EXHAUSTION is not None:
        STRATEGIES[_RESOURCE_EXHAUSTION] = ["bounded_cache"]

    def __init__(self, target_file: Path | str = "target_app/app.py") -> None:
        self.target_file = Path(target_file)

    def _relative_target(self) -> str:
        root = Path(__file__).resolve().parents[2]
        try:
            return self.target_file.resolve().relative_to(root).as_posix()
        except ValueError:
            return self.target_file.name

    def _transform(self, bug_class: BugClass, strategy_index: int, source: str) -> str:
        module = cst.parse_module(source)
        if bug_class is BugClass.PANIC:
            module = module.visit(
                PanicGuardTransformer() if strategy_index == 0 else PanicBoundaryTransformer()
            )
            module = _ensure_import(module, _json_response_import())
        elif bug_class is BugClass.STARVATION:
            module = module.visit(
                AsyncLockTransformer() if strategy_index == 0 else ThreadPoolTransformer()
            )
            module = _ensure_import(module, _asyncio_import())
        elif bug_class is BugClass.UPSTREAM_TIMEOUT:
            module = module.visit(TimeoutTransformer(add_boundary=strategy_index == 0))
            module = _ensure_import(module, _json_response_import())
            if strategy_index == 1:
                module = module.visit(FallbackTransformer())
        elif self._RESOURCE_EXHAUSTION is not None and bug_class is self._RESOURCE_EXHAUSTION:
            transformer = BoundedCacheTransformer()
            module = module.visit(transformer)
            if transformer.cache_names:
                module = _ensure_import(
                    module,
                    cst.parse_statement("from collections import OrderedDict\n"),  # type: ignore[return-value]
                )
            if transformer.deque_names:
                module = _ensure_import(
                    module,
                    cst.parse_statement("from collections import deque\n"),  # type: ignore[return-value]
                )
        else:
            raise ValueError(f"Unknown bug class: {bug_class}")
        return module.code

    def apply_patch(
        self, bug_class: BugClass, sig_id: str, strategy_index: int = 0
    ) -> PatchResult:
        if bug_class not in self.STRATEGIES:
            raise ValueError(f"Unknown bug class: {bug_class}")
        strategy_index = max(0, min(strategy_index, len(self.STRATEGIES[bug_class]) - 1))
        strategy = self.STRATEGIES[bug_class][strategy_index]
        original = self.target_file.read_text(encoding="utf-8")
        modified = self._transform(bug_class, strategy_index, original)
        compile(modified, str(self.target_file), "exec")
        diff = "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                modified.splitlines(keepends=True),
                fromfile=f"a/{self._relative_target()}",
                tofile=f"b/{self._relative_target()}",
            )
        )
        self.target_file.write_text(modified, encoding="utf-8")
        return PatchResult(
            sig_id=sig_id,
            strategy=strategy,
            file_path=str(self.target_file),
            diff=diff,
            applied=original != modified,
            base_file_sha256=hashlib.sha256(original.encode("utf-8")).hexdigest(),
        )

    def rollback(self, pristine_content: str) -> None:
        self.target_file.write_text(pristine_content, encoding="utf-8")
