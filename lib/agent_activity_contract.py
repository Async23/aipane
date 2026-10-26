"""Shared tmux projection names without loading activity evidence detectors."""

ACTIVITY_MARKER_OPTION = "@tmux-window-wrap-activity"
ACTIVITY_REPORTER_OPTION = "@tmux-window-wrap-activity-reporter"
ACTIVITY_UPDATED_AT_OPTION = "@tmux-window-wrap-activity-updated-at"
ACTIVITY_RECORD_OPTION = "@tmux-window-wrap-activity-record"
ACTIVITY_OPTIONS = (
    ACTIVITY_MARKER_OPTION,
    ACTIVITY_REPORTER_OPTION,
    ACTIVITY_UPDATED_AT_OPTION,
    ACTIVITY_RECORD_OPTION,
)
ACTIVITY_FIELD_SEPARATOR = "\x1f"


class ActivityError(RuntimeError):
    """Base error for the Agent Activity interface."""
