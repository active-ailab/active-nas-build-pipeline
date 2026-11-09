"""Optional release subtasks report errors without failing firmware publishing."""
import sys
import traceback


def record_subtask_error(cfg, name, error):
    cfg.setdefault("subtask_errors", {})[name] = f"{type(error).__name__}: {error}"
    print(f"[{name.upper()}:ERROR] {type(error).__name__}: {error}; 主发布流程继续", file=sys.stderr, flush=True)
    traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)


def failed_optional_placeholder(cfg, key):
    errors = cfg.get("subtask_errors") or {}
    return bool((errors.get("tscan") and key.startswith("{{REL_TSCAN_"))
                or (errors.get("changelog") and key == "{{REL_CHANGELOG_LINK}}"))
