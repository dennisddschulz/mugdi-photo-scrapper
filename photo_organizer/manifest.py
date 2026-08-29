"""Persist the plan, and round-trip user-edited event names (R-F5, R-F8, R-N6).

These are the only functions in the planning stage that write anything, and
they refuse to write inside the source tree.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import read_toml
from .models import Event, Plan
from .naming import sanitize_label
from .scan import UnsafePathError, _is_within, _resolve


log = logging.getLogger(__name__)


def guard_write_target(path: Path, source_root: Path) -> Path:
    """Refuse to write anywhere inside the read-only source tree (R-S2)."""
    resolved = _resolve(path)
    if _is_within(resolved, _resolve(source_root)):
        raise UnsafePathError(
            f"Refusing to write {resolved}: it is inside the source tree "
            f"({source_root}). The source is treated as read-only."
        )
    return resolved


def save_manifest(plan: Plan, path: Path) -> Path:
    """Write the full plan as JSON."""
    target = guard_write_target(path, plan.source_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(plan.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    log.info("Manifest written to %s", target)
    return target


def _toml_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


@dataclass
class PlanEdits:
    """User decisions about a plan: renames, and merges into the previous event."""

    names: dict[int, str] = field(default_factory=dict)
    merges: set[int] = field(default_factory=set)

    def __bool__(self) -> bool:
        return bool(self.names or self.merges)


def write_edits_file(
    plan: Plan,
    path: Path,
    names: Optional[dict[int, str]] = None,
    merges: Optional[set[int]] = None,
) -> Path:
    """Export event names (and merge flags) as an editable TOML file.

    Always written against the ORIGINAL clustering indices, so the file
    stays meaningful when re-applied to a freshly built plan.
    """
    target = guard_write_target(path, plan.source_root)
    target.parent.mkdir(parents=True, exist_ok=True)

    events = plan.pre_edit_events or plan.events
    names = names or {}
    merges = merges or set()

    lines = [
        "# Event plan edits - change `name`, then re-run with",
        "#   --names " + target.name,
        "# Set `merge_into_previous = true` to fold an event into the one",
        "# above it, when the clustering split something it should not have.",
        "# Only `name` and `merge_into_previous` are read back; the rest is",
        "# context for you. Nothing is copied by editing this file.",
        f"# Generated {datetime.now():%Y-%m-%d %H:%M} from {plan.source_root}",
        "",
    ]
    for event in events:
        start = event.start
        chosen = names.get(event.index, event.effective_name)
        lines.append("[[events]]")
        lines.append(f"index = {event.index}")
        lines.append(f'name = "{_toml_escape(chosen)}"')
        if event.index in merges:
            lines.append("merge_into_previous = true")
        lines.append(f'proposed = "{_toml_escape(event.proposed_name or "")}"')
        lines.append(f'year = "{event.year}"')
        lines.append(f"photo_count = {len(event.photos)}")
        if start:
            lines.append(f'starts = "{start:%Y-%m-%d %H:%M}"')
        if event.place_label:
            lines.append(f'place = "{_toml_escape(event.place_label)}"')
        for note in event.notes:
            lines.append(f"# note: {note}")
        lines.append("")

    target.write_text("\n".join(lines), encoding="utf-8")
    log.info("Edits file written to %s", target)
    return target


def write_names_file(plan: Plan, path: Path) -> Path:
    """Export proposed event names with no edits applied."""
    return write_edits_file(plan, path)


def load_edits_file(path: Path) -> PlanEdits:
    """Read user names and merge flags back in."""
    data = read_toml(path)
    edits = PlanEdits()

    for entry in data.get("events", []):
        index = entry.get("index")
        if index is None:
            continue
        index = int(index)

        if entry.get("merge_into_previous"):
            edits.merges.add(index)

        raw = (entry.get("name") or "").strip()
        if not raw:
            continue
        clean = sanitize_label(raw)
        if not clean:
            log.warning(
                "Event %s: name %r is unusable as a folder name; ignoring", index, raw
            )
            continue
        if clean != raw:
            log.info("Event %s: name %r sanitized to %r", index, raw, clean)
        edits.names[index] = clean
    return edits


def load_names_file(path: Path) -> dict[int, str]:
    """Read only the names back in. Returns {event_index: name}."""
    return load_edits_file(path).names


def apply_names(plan: Plan, names: dict[int, str]) -> int:
    """Apply user names to the plan. Returns how many events changed."""
    changed = 0
    for event in plan.events:
        chosen = names.get(event.index)
        if chosen and chosen != event.proposed_name:
            event.user_name = chosen
            event.notes.append(f"renamed by user (was {event.proposed_name})")
            changed += 1
    return changed


def apply_edits(plan: Plan, edits: PlanEdits) -> tuple[int, int]:
    """Apply names and merges to a plan in place.

    Returns (renamed_count, merged_count). Merging folds an event's photos
    into the previous surviving event; the absorbing event keeps its own
    index and name, so the result stays addressable.
    """
    # Import here: planner imports manifest indirectly via the CLI, and this
    # keeps the module graph acyclic.
    from .planner import assign_dest_names

    renamed = apply_names(plan, edits.names)

    merged = 0
    if edits.merges:
        plan.pre_edit_events = list(plan.events)
        kept: list[Event] = []
        for event in plan.events:
            if event.index in edits.merges and kept:
                target = kept[-1]
                target.photos.extend(event.photos)
                target.notes.append(
                    f"merged event {event.index} "
                    f"({len(event.photos)} photo(s)) in, at your request"
                )
                merged += 1
                continue
            if event.index in edits.merges and not kept:
                event.notes.append(
                    "merge_into_previous ignored: this is the first event"
                )
            kept.append(event)
        plan.events = kept
        # Photos moved between events, so destination names must be redone.
        for event in plan.events:
            assign_dest_names(event)

    return renamed, merged


def load_manifest(path: Path) -> dict:
    """Read a manifest back as raw JSON (used by later milestones)."""
    return json.loads(Path(path).read_text(encoding="utf-8"))
