# FastFix Benchmarks

This directory contains transparent, synthetic repair tasks for evaluating
FastFix. Each task includes a buggy fixture repository, issue description,
machine-readable metadata, and a verified gold patch.

Validate FF-001 with:

```powershell
.\.venv\Scripts\python.exe benchmarks\scripts\validate_ff001.py
```
