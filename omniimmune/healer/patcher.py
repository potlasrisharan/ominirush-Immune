from __future__ import annotations

import difflib
import hashlib
from pathlib import Path

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


class PatchLadder:
    STRATEGIES = {
        BugClass.PANIC: ["input_guard", "try_catch_boundary"],
        BugClass.STARVATION: ["asyncio_lock_nonblocking", "thread_pool_offload"],
        BugClass.UPSTREAM_TIMEOUT: ["explicit_timeout_guard", "circuit_breaker_fallback"],
    }

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
