"""Data pipeline orchestration: fetch → normalize → cache (docs/data-layer.md §3).

Not yet implemented. This package will own the read-through cache and the
`discover` / `validate` / `watch` fetch sequences; it is the only caller of
`providers/`.
"""
