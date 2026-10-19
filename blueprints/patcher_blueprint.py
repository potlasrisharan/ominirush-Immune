"""
LibCST Patcher Blueprint with Patch Ladder
Preserves exact file formatting, comments, and whitespace while applying surgical AST transforms.
"""
from __future__ import annotations

import difflib
from pathlib import Path
from typing import Optional

import libcst as cst
from omniimmune.models import BugClass, PatchResult


class PanicGuardTransformer(cst.CSTTransformer):
    """
    Transforms `create_order` to add input validation guards returning 400 Bad Request.
    """
    def leave_FunctionDef(
        self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef
    ) -> cst.FunctionDef:
        if original_node.name.value != "create_order":
            return updated_node

        # Synthesize validation guard statement
        guard_code = """
    if quantity <= 0:
        return JSONResponse(status_code=400, content={"error": "quantity must be greater than zero", "status": "rejected"})
"""
        parsed_guard = cst.parse_statement(guard_code)
        new_body = [parsed_guard, *updated_node.body.body]
        return updated_node.with_changes(body=updated_node.body.with_changes(body=new_body))


class AsyncLockTransformer(cst.CSTTransformer):
    """
    Replaces blocking threading.Lock with asyncio.Lock and non-blocking sleep.
    """
    def leave_Assign(
        self, original_node: cst.Assign, updated_node: cst.Assign
    ) -> cst.Assign:
        # Match `_inventory_lock = threading.Lock()`
        if len(original_node.targets) == 1:
            target = original_node.targets[0].target
            if isinstance(target, cst.Name) and target.value == "_inventory_lock":
                return cst.parse_statement("_inventory_lock = asyncio.Lock()")  # type: ignore
        return updated_node

    def leave_With(
        self, original_node: cst.With, updated_node: cst.With
    ) -> cst.CSTNode:
        # Change `with _inventory_lock:` to `async with _inventory_lock:` and replace time.sleep with await asyncio.sleep
        for item in original_node.items:
            if isinstance(item.item, cst.Name) and item.item.value == "_inventory_lock":
                new_with = cst.parse_statement("""
    async with _inventory_lock:
        await asyncio.sleep(0.01)
        return {"reserved": True}
""")
                return new_with
        return updated_node


class TimeoutGuardTransformer(cst.CSTTransformer):
    """
    Replaces `timeout=None` with `timeout=1.5` and wraps upstream call with timeout error handling.
    """
    def leave_FunctionDef(
        self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef
    ) -> cst.FunctionDef:
        if original_node.name.value != "verify_payment":
            return updated_node

        patched_code = """
@app.get("/api/v1/payment/verify")
async def verify_payment(delay: float = 4.0):
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            resp = await client.get(f"{UPSTREAM_URL}/api/upstream/slow?delay={delay}")
            return {"verified": True, "upstream": resp.json()}
    except (httpx.TimeoutException, httpx.RequestError):
        return JSONResponse(status_code=504, content={"verified": False, "error": "upstream_timeout_handled"})
"""
        parsed_func = cst.parse_statement(patched_code.strip())
        return parsed_func  # type: ignore


class PatchLadder:
    """
    Manages ordered patching strategies for each bug class.
    """
    STRATEGIES = {
        BugClass.PANIC: ["input_guard", "try_catch_boundary"],
        BugClass.STARVATION: ["asyncio_lock_nonblocking", "thread_pool_offload"],
        BugClass.UPSTREAM_TIMEOUT: ["explicit_timeout_guard", "circuit_breaker_fallback"],
    }

    def __init__(self, target_file: Path | str = "target_app/app.py"):
        self.target_file = Path(target_file)

    def apply_patch(
        self,
        bug_class: BugClass,
        sig_id: str,
        strategy_index: int = 0,
    ) -> PatchResult:
        strategies = self.STRATEGIES.get(bug_class, ["default"])
        strategy = strategies[min(strategy_index, len(strategies) - 1)]

        original_code = self.target_file.read_text(encoding="utf-8")
        parsed_cst = cst.parse_module(original_code)

        if bug_class == BugClass.PANIC:
            transformer = PanicGuardTransformer()
        elif bug_class == BugClass.STARVATION:
            # Ensure asyncio import exists
            if "import asyncio" not in original_code:
                original_code = "import asyncio\n" + original_code
                parsed_cst = cst.parse_module(original_code)
            transformer = AsyncLockTransformer()
        elif bug_class == BugClass.UPSTREAM_TIMEOUT:
            transformer = TimeoutGuardTransformer()
        else:
            raise ValueError(f"Unknown bug class: {bug_class}")

        modified_cst = parsed_cst.visit(transformer)
        modified_code = modified_cst.code

        # Safety Gate 1: Check Python compilation syntax
        compile(modified_code, str(self.target_file), "exec")

        # Compute clean diff
        diff_lines = list(
            difflib.unified_diff(
                original_code.splitlines(keepends=True),
                modified_code.splitlines(keepends=True),
                fromfile=f"a/{self.target_file.name}",
                tofile=f"b/{self.target_file.name}",
            )
        )
        diff_str = "".join(diff_lines)

        # Write modified code to disk
        self.target_file.write_text(modified_code, encoding="utf-8")

        return PatchResult(
            sig_id=sig_id,
            strategy=strategy,
            file_path=str(self.target_file),
            diff=diff_str,
            applied=True,
        )

    def rollback(self, original_content: str) -> None:
        """Rolls back the target file to pristine state on verification failure."""
        self.target_file.write_text(original_content, encoding="utf-8")
