"""
Stretch scope (section 3.2): schema drift detection via information_schema
snapshot diffing. Compares two {column_name: column_type} snapshots (see
connectors.snapshot_schema) and reports additions, removals, and type changes.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SchemaDiff:
    added_columns: list[str] = field(default_factory=list)
    removed_columns: list[str] = field(default_factory=list)
    type_changes: dict[str, tuple[str, str]] = field(default_factory=dict)  # col -> (old, new)

    @property
    def has_drift(self) -> bool:
        return bool(self.added_columns or self.removed_columns or self.type_changes)

    def summary(self) -> str:
        if not self.has_drift:
            return "No schema drift detected."
        parts = []
        if self.added_columns:
            parts.append(f"added columns: {', '.join(self.added_columns)}")
        if self.removed_columns:
            parts.append(f"removed columns: {', '.join(self.removed_columns)}")
        if self.type_changes:
            changes = ", ".join(f"{c}: {old} -> {new}" for c, (old, new) in self.type_changes.items())
            parts.append(f"type changes: {changes}")
        return "Schema drift detected - " + "; ".join(parts)


def diff_schema(old_snapshot: dict[str, str], new_snapshot: dict[str, str]) -> SchemaDiff:
    diff = SchemaDiff()
    old_cols, new_cols = set(old_snapshot), set(new_snapshot)
    diff.added_columns = sorted(new_cols - old_cols)
    diff.removed_columns = sorted(old_cols - new_cols)
    for col in old_cols & new_cols:
        if old_snapshot[col] != new_snapshot[col]:
            diff.type_changes[col] = (old_snapshot[col], new_snapshot[col])
    return diff
