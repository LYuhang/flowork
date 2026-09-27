"""Stable JSONL records for synchronous Workflow and batch CLI executions."""

def _jsonl_record(index: int, input_row: dict, rj: dict | None, error: str | None = None) -> dict:
    if rj is None:
        return {
            "index": index,
            "status": "error",
            "input": input_row,
            "output": None,
            "node_outputs": {},
            "errors": {"_row_error": error or "no result"},
            "execution_time": 0.0,
        }
    node_outputs = rj.get("final_outputs") or {}
    errors = {k: str(v) for k, v in (rj.get("error_dict") or {}).items()}
    return {
        "index": index,
        "status": "error" if errors else "success",
        "input": input_row,
        "output": node_outputs.get("__end__"),
        "node_outputs": node_outputs,
        "errors": errors,
        "execution_time": round(rj.get("execution_time", 0.0) or 0.0, 3),
    }
