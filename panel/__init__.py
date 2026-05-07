"""Public API of the panel package.

The panel was split into four submodules along responsibility boundaries:
  - render: pure embed/text formatting
  - writer: per-guild coalescing message-edit task + ticker
  - view:   discord.py UI classes (QueueControlsView, Selects, Modal)
  - lifecycle: panel cleanup, interaction acks, idle disconnect

External callers should import from `panel` (this module) — the submodule
layout is implementation detail.
"""
from panel.render import (
    build_panel_embed,
    format_now_playing,
    format_queue_lines,
)
from panel.view import (
    QueueControlsView,
    make_controls,
)
from panel.writer import (
    ensure_panel_writer,
    refresh_panel,
    request_panel_update,
    start_now_playing_ticker,
    stop_panel_writer,
)
from panel.lifecycle import (
    cancel_inactivity,
    clear_panel,
    schedule_inactivity,
    silent_ack,
)


__all__ = [
    "QueueControlsView",
    "build_panel_embed",
    "cancel_inactivity",
    "clear_panel",
    "ensure_panel_writer",
    "format_now_playing",
    "format_queue_lines",
    "make_controls",
    "refresh_panel",
    "request_panel_update",
    "schedule_inactivity",
    "silent_ack",
    "start_now_playing_ticker",
    "stop_panel_writer",
]
