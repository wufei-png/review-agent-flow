# Agent-owned output and Auto-fix Merge Requests

Agents directly submit GitLab Notes and do not need to return a canonical JSON result. If auto-fix is enabled, the Agent creates an independent MR targeting the original source branch for the original MR creator to review.
