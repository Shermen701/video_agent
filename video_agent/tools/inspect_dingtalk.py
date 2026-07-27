"""Write a UIA/Win32 inspection report for visible DingTalk windows."""
from __future__ import annotations

import argparse
import time
from pathlib import Path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="test_outputs/dingtalk_inspect")
    parser.add_argument("--wait-seconds", type=float, default=0)
    parser.add_argument("--include-unnamed", action="store_true")
    parser.add_argument("--backend", choices=("both", "uia", "win32"), default="both")
    args = parser.parse_args(argv)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if args.wait_seconds > 0:
        time.sleep(args.wait_seconds)
    try:
        from pywinauto import Desktop  # type: ignore
    except ModuleNotFoundError as exc:
        raise SystemExit("pywinauto is required; install requirements.txt") from exc

    lines = ["DingTalk UI inspection"]
    found_any = False
    backends = ("uia", "win32") if args.backend == "both" else (args.backend,)
    for backend in backends:
        try:
            windows = Desktop(backend=backend).windows(visible_only=True)
        except Exception as exc:
            lines.append(f"backend={backend} window_lookup_error={exc}")
            continue
        matches = [window for window in windows if _is_dingtalk_window(window)]
        found_any = found_any or bool(matches)
        for index, window in enumerate(matches):
            _inspect_window(lines, output_dir, backend, index, window, args.include_unnamed)
    if not found_any:
        lines.append("No visible DingTalk window found.")
    report = output_dir / "dingtalk-ui-inspect.txt"
    report.write_text("\n".join(lines), encoding="utf-8")
    print(report.resolve())


def _is_dingtalk_window(window) -> bool:
    try:
        title = str(window.window_text() or "").casefold()
        auto_id = str(window.element_info.automation_id or "").casefold()
        return (
            any(token in title for token in ("钉钉", "dingtalk", "ding"))
            or auto_id.startswith("dt_main_frame_view")
            or auto_id.startswith("prepareframev2")
            or auto_id.startswith("loginview")
        )
    except Exception:
        return False


def _inspect_window(
    lines: list[str], output_dir: Path, backend: str, index: int, window, include_unnamed: bool
) -> None:
    try:
        rect = window.rectangle()
        bounds = f"({rect.left},{rect.top})-({rect.right},{rect.bottom})"
    except Exception:
        bounds = "unavailable"
    lines.append(
        f"backend={backend} window[{index}] title={window.window_text()!r} "
        f"class={window.element_info.class_name!r} auto_id={window.element_info.automation_id!r} bounds={bounds}"
    )
    try:
        window.capture_as_image().save(output_dir / f"dingtalk-{backend}-window-{index}.png")
    except Exception as exc:
        lines.append(f"backend={backend} window[{index}] screenshot_error={exc}")
    try:
        controls = window.descendants()
    except Exception as exc:
        lines.append(f"backend={backend} window[{index}] descendants_error={exc}")
        return
    for control in controls[:2000]:
        try:
            name = str(getattr(control.element_info, "name", "") or "")
            text = str(control.window_text() or "")
            auto_id = str(getattr(control.element_info, "automation_id", "") or "")
            class_name = str(getattr(control.element_info, "class_name", "") or "")
            if not (name or text or auto_id or include_unnamed):
                continue
            control_type = str(getattr(control.element_info, "control_type", "") or "")
            try:
                rect = control.rectangle()
                bounds = f"({rect.left},{rect.top})-({rect.right},{rect.bottom})"
            except Exception:
                bounds = "unavailable"
            lines.append(
                f"  text={text!r} name={name!r} auto_id={auto_id!r} "
                f"class={class_name!r} type={control_type!r} bounds={bounds}"
            )
        except Exception:
            continue


if __name__ == "__main__":
    main()
