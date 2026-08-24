# Public repository snapshot boundary

This repository is the public, reviewable Agent-loop source snapshot. It does
not copy the local runtime cache, the complete screenshot archive, or the
firmware workspaces into Git history.

## Included in this repository

- Agent-loop source under `src/`;
- frontend, simulator tools, tests, case maps, and project configuration;
- current engineering and hardware notes under `docs/`;
- this snapshot boundary and the current local-source baseline.

The current checkout is published from the existing working branch. The
working-tree changes are staged by explicit source/document/test paths; local
temporary files are not included.

## Local data represented by this snapshot

The following local roots remain outside the public repository. Their size and
purpose are recorded here so another checkout can distinguish missing source
from intentionally excluded runtime data.

| Local root | Observed contents | Approximate size | Public-repository treatment |
| --- | ---: | ---: | --- |
| `D:\Agent-loop-evidence` | 3,525 files, primarily BMP/JSON evidence | 1.85 GiB | Excluded as a bulk archive; use the existing evidence and screenshot notes in `docs/`. |
| `D:\Agent-loop-system` | 40,572 files including source plus runtime/history/artifacts | 3.79 GiB | Source, tests, docs, case maps, and configuration are included; `.runtime/`, `artifacts/`, `evidence/`, `history/`, `logs/`, `defects/`, `.venv/`, caches, and other generated data remain ignored. |
| `D:\Agent-loop-workspace` | firmware/simulator workspaces and SuperCom bridge | 11.48 GiB | Excluded as a bulk archive; only this inventory and the source provenance notes are published. |

## Workspace inventory at publication time

The firmware directories contain local modifications and are not copied into
this public repository. These commit IDs are an inventory only, not a claim
that the corresponding worktree is clean or reproducibly buildable.

| Workspace | Git state | Local status |
| --- | --- | --- |
| `6202_W5230` | `b2b25798fcce71fae542e129308bacc8d6536b23` (detached) | modified |
| `6202_W5230_SIM_02` | no Git metadata | local simulator copy |
| `6204_W5230` | `14b6116a55897be9ff8afb293002bb3dbeecb4ca` (detached) | modified |
| `620C_W6830` | `0679ccc319a34981d4d7476ff080c039c8e06c07` (detached) | modified |
| `SuperCom-AgentBridge` | `5cc3948e5a55085a6b630847bc5aabf31591f9bf` (`agentbridge`) | modified |

## Explicit exclusions

The public snapshot excludes firmware/build outputs and local tooling caches,
including `.git`, `.svn`, build/output directories, binaries and libraries,
serial logs, complete screenshot archives, virtual environments, and private
or signing-key material. Files over GitHub's ordinary 100 MB file limit are not
checked in; this repository does not silently convert them to Git LFS objects.

This boundary keeps the public repository reviewable and prevents a local
device workspace or credential-like material from becoming part of the public
history. A complete archival copy, if needed later, must use a private,
access-controlled artifact store with a separately reviewed allow-list.
