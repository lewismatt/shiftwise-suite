"""Coworker invite lifecycle shared by staffing and request invalidation paths."""


def reconcile_swap_invites(conn, shift_ids):
    """Restore the invite/marker invariant before committing staffing changes.

    The caller must already hold every supplied source shift's transaction lock.
    Never take new locks here: paired responses, rebuilds and cascading deletes
    acquire shift locks in ID order before touching either table. An invalidated
    source assignment cancels its invite; a cancelled invite releases its holder
    back to confirmed staffing, without recreating deleted/absent assignments.
    """
    ids = sorted({sid for sid in shift_ids if sid is not None})
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    conn.execute(
        "UPDATE requests r SET status='superseded', "
        "reason='The source assignment is no longer available for this swap.' "
        f"WHERE r.shift_id IN ({placeholders}) AND r.kind='swap' "
        "AND r.target_user_id IS NOT NULL AND r.status='approved' "
        "AND NOT EXISTS (SELECT 1 FROM assignments a WHERE a.shift_id=r.shift_id "
        "AND a.user_id=r.user_id AND a.status='swap_invited')",
        ids,
    )
    conn.execute(
        f"UPDATE assignments a SET status='confirmed' WHERE a.shift_id IN ({placeholders}) "
        "AND a.status='swap_invited' AND NOT EXISTS (SELECT 1 FROM requests r "
        "WHERE r.shift_id=a.shift_id AND r.user_id=a.user_id AND r.kind='swap' "
        "AND r.target_user_id IS NOT NULL AND r.status='approved')",
        ids,
    )
