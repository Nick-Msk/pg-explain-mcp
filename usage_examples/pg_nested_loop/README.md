> **note.** `NestedLoopCheck` reports at `INFO` level. It does **not**
> signal a problem — a Nested Loop with an indexed inner side is often
> optimal. The check is a heads-up for future growth, not a fix-me
> warning. If you find it noisy, plan for the configurable-checks
> feature described in the main [`README.md`](../../README.md#roadmap)
.
