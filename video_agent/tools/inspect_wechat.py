"""Capture the visible WeChat window tree for selector calibration.

Run this only from the unlocked Windows desktop session that owns the logged-in
WeChat client. It never clicks controls or changes WeChat state.
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="test_outputs/wechat_inspect")
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=0,
        help="Wait before inspection so WeChat can be brought to the foreground.",
    )
    parser.add_argument(
        "--include-unnamed",
        action="store_true",
        help="Also list unnamed controls with their bounds and control type for icon calibration.",
    )
    args = parser.parse_args(argv)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if args.wait_seconds > 0:
        time.sleep(args.wait_seconds)
    try:
        from pywinauto import Desktop  # type: ignore
    except ModuleNotFoundError as exc:
        raise SystemExit("pywinauto is required; install requirements.txt") from exc

    lines = ["WeChat UI inspection"]
    found_any = False
    for backend in ("uia", "win32"):
        try:
            windows = list(Desktop(backend=backend).windows(visible_only=True))
        except Exception as exc:
            lines.append(f"backend={backend} window_lookup_error={exc}")
            continue
        matched = [
            window
            for window in windows
            if any(token in (window.window_text() or "").lower() for token in ("微信", "wechat", "weixin"))
        ]
        found_any = found_any or bool(matched)
        for index, window in enumerate(matched):
            _inspect_window(lines, output_dir, backend, index, window, args.include_unnamed)
    if not found_any:
        lines.append("No visible WeChat window found. Run this in the unlocked interactive desktop session.")
    report = output_dir / "wechat-ui-inspect.txt"
    report.write_text("\n".join(lines), encoding="utf-8")
    print(report.resolve())


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
        window.capture_as_image().save(output_dir / f"wechat-{backend}-window-{index}.png")
    except Exception as exc:
        lines.append(f"backend={backend} window[{index}] screenshot_error={exc}")
    try:
        controls = window.descendants()
    except Exception as exc:
        lines.append(f"backend={backend} window[{index}] descendants_error={exc}")
        return
    for control in controls[:1000]:
        try:
            name = str(getattr(control.element_info, "name", "") or "")
            text = str(control.window_text() or "")
            auto_id = str(getattr(control.element_info, "automation_id", "") or "")
            class_name = str(getattr(control.element_info, "class_name", "") or "")
            if name or text or auto_id or include_unnamed:
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
