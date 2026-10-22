> Historical v0.3 design. V1 uses SQLite and Dev Tunnels; see ../../README.md.

# Sahakaram database design

Status: fourteen application tables are implemented in Alembic revision `0001`.
The immutable schema snapshot is `src/sahakaram/storage/schema_v1.py` and the
current runtime imports it through `schema.py`. General rooms allow a NULL
`project_id`; `github_user_id` is nullable until GitHub integration. The catalog
supports multiple repositories per project, but project admission is deliberately
refused until permission verification is implemented in Phase 6.

This document retains the full target design. The current API implements local
creation/admission, invitations, messages, task transitions, revocation, replay,
and account usage observations. Re-enrollment, role transfer, policy scheduling,
GitHub verification, retention, and load benchmarks remain future work. See
`phases-1-3.md` for tested implementation scope and actual constraints.

## Requirements and boundaries

- PostgreSQL is the authoritative store for Sahakaram's collaboration state.
- The users table contains exactly id and displayname. Display names need not be unique and are not authentication identifiers.
- A user owns multiple provider accounts; each account has its own reported allowance. Account ownership is immutable in v1.
- Projects contain multiple repositories. Repository records can be reused across projects through a junction table.
- A project can have multiple rooms. Human dashboard membership and agent participation have separate permissions and lifecycles.
- There is no devices table and no access_credentials table.
- Each room initially permits three agent participants, including the lead. Human dashboard members do not consume agent seats.
- Every participant must have the required GitHub access to every repository in the project. PostgreSQL records collaboration state; GitHub remains authoritative about current repository permissions.
- Same-user linking is scoped to a human membership in a room in v1. It links local account registrations; it does not establish provider identity or combine quotas.

## Normalization decisions

The design separates independently owned facts and uses junction/child rows for repeated relationships. It does not claim that every table is strictly in third normal form: room_id is deliberately retained on scoped records alongside participant or membership references so composite foreign keys can prohibit cross-room references. This is a bounded integrity-oriented duplication, not a second editable source of room membership.

Changes from the previous proposal:

1. Repositories no longer contain project_id. A GitHub repository has one canonical row; project_repositories records its project associations.
2. Usage windows are rows in usage_windows, not a JSON array. Multiple reset windows therefore remain independently queryable and constrained.
3. repository_access is removed. Permanent permission rows would only be a cache of GitHub permissions and could misleadingly appear authoritative. Access checks are performed at admission/work boundaries and logged as events.
4. Participant rows do not copy user_id or displayname. The owning user is obtained through agent_accounts. A participant's chosen github_user_id identifies the GitHub identity used in that room; it is independent of the Codex/Claude account.
5. Repeated provider names, fixed role names, and fixed statuses do not need lookup tables in v1. Use text values with CHECK constraints, changed through migrations.
6. Counters and versions are explicit operational state, not separately maintained dashboard totals. Online/working state and allowance remaining are derived rather than duplicated.

## Tables and keys

Notation: PK = primary key; FK = foreign key; UQ = unique constraint; ? = nullable. Object IDs are UUIDs; external GitHub IDs and event sequences are bigint. Event, message, verification, and observation timestamps use timestamptz. Only users is intentionally timestamp-free.

### Identity and project catalog

| Table | Columns | Keys and rules |
| --- | --- | --- |
| users | id, displayname | PK id; nonempty displayname; exactly these two columns |
| agent_accounts | id, user_id, provider, label, provider_account_ref? | PK id; FK user_id to users; UQ(provider, provider_account_ref) when the reference is available; provider credentials are not stored here |
| projects | id, name, description? | PK id; nonempty name; project names need not be globally unique |
| repositories | id, github_repository_id, owner, name | PK id; UQ github_repository_id; owner/name are the most recently resolved display location, not the permanent identity |
| project_repositories | project_id, repository_id | Composite PK(project_id, repository_id); FKs to projects and repositories |

Provider account references are populated only from adapter-verified provider identity when available. A label alone cannot establish that two registrations have separate quota pools. Unknown references remain NULL, not fabricated IDs. No GitHub tokens or provider login secrets are stored in these catalog tables.

### Rooms and participation

| Table | Columns | Keys and rules |
| --- | --- | --- |
| rooms | id, project_id, name, status, max_participants, instructions, communication_interval_seconds, usage_limit_policy, policy_version, last_event_sequence | PK id; FK project_id; max_participants > 0; interval > 0; event sequence initially 0 |
| room_memberships | id, room_id, user_id, role, status, token_version, link_secret_hash? | PK id; FKs room_id and user_id; UQ(room_id, user_id); UQ(room_id, id) for scoped FKs |
| participants | id, room_id, account_id, github_user_id, role, status, token_version, last_seen_at? | PK id; FKs room_id and account_id; UQ(room_id, account_id); UQ(room_id, id) for scoped FKs |

Room states: active, draining, paused, closed. Human roles: admin, member. Human membership states: active, left, revoked. Agent roles: lead, teammate. Agent participation states: pending_access, active, left, kicked. Pending and active participants both reserve seats; disconnection does not release a seat. Working state is obtained from tasks; connectivity is obtained from the companion connection and last_seen_at.

Use a partial unique index to allow at most one active admin per room and another to allow at most one reserved/active lead per room. Creation, transfer, departure, and closure transactions maintain the stronger requirement that an open room has an active admin, and work dispatch requires an active lead. Rejoining reuses the existing membership/participant row and increments token_version; join/leave history is retained in room_events.

Participants do not store membership_id or a duplicate user_id. Admission checks that the account owner has an active room_memberships row. Token authorization checks both participant state and its owner's membership. Account ownership cannot be silently transferred to bypass this relationship.

link_secret_hash is a password-hashed verifier used only for optional same-user enrollment under this room membership. It is not a provider password or a bearer token. Normal joining uses invitations; displayname never authenticates a caller.

### Work and conversation

| Table | Columns | Keys and rules |
| --- | --- | --- |
| tasks | id, room_id, repository_id?, parent_task_id?, assigned_participant_id?, title, description, status, result_summary?, work_branch?, result_commit_sha?, version, updated_at | PK id; UQ(room_id, id); FK repository_id; composite same-room FKs for parent and assignee |
| messages | id, room_id, sender_membership_id?, sender_participant_id?, recipient_participant_id?, task_id?, content, created_at | PK id; FK room_id; exactly one sender; composite same-room FKs for senders, recipient, and task |

Task states: queued, assigned, working, blocked, review, done, cancelled. Unassigned tasks have no assignee. A task with repository_id NULL is coordination work; implementation tasks target one repository. Multi-repository work is a parent task with repository-specific children. Parent references are chosen at creation and are immutable in v1, avoiding cycles without a separate dependency graph subsystem.

work_branch is the selected working branch. result_commit_sha is a completion/head reference, not an exhaustive commit history; Git remains authoritative for the commit graph. Multiple branches or commits are not packed into comma-separated fields.

Task version is incremented on edits and assignment transitions. Updates compare the expected version so a stale agent cannot overwrite a newer assignment. Cross-room references are rejected by composite FKs. Whether a task repository belongs to the room's project is checked transactionally through rooms and project_repositories.

### Usage reporting

| Table | Columns | Keys and rules |
| --- | --- | --- |
| usage_snapshots | id, account_id, observed_at, source, availability, error_code? | PK id; FK account_id; availability is available or unavailable |
| usage_windows | snapshot_id, limit_id, window_key, used_percent?, resets_at?, duration_seconds? | Composite PK(snapshot_id, limit_id, window_key); FK snapshot_id; percentage between 0 and 100 when present; positive duration when present |

Every adapter poll produces one snapshot. An unsuccessful poll records unavailable and no windows; a successful poll must contain at least one meaningful window. Inserting the snapshot and windows is one transaction. Adapter validation enforces the parent/child availability rule.

Remaining percentage is computed as 100 minus used_percent. Do not store both. NULL is unknown, not zero. Freshness is computed from observed_at, not persisted as another status flag. The latest failure must not cause older successful data to be displayed as current; the dashboard may show older values only with their age and a stale label.

These v1 columns support percentage windows reported by the provider adapter. If a later provider exposes different units, extend the explicit window model for its real semantics rather than inventing percentages.

### Enrollment and delivery

| Table | Columns | Keys and rules |
| --- | --- | --- |
| invitations | id, room_id, kind, token_hash, expires_at, max_uses, use_count, revoked_at? | PK id; FK room_id; UQ token_hash; kind dashboard or agent; max_uses > 0; 0 <= use_count <= max_uses |
| room_events | room_id, sequence, kind, payload, created_at | Composite PK(room_id, sequence); FK room_id; payload JSONB contains event-specific data |

Invitations contain hashes of random secrets, not plaintext links. Session tokens are short-lived signed tokens; the signing key lives in server configuration. Tokens identify a membership or participant and carry its token_version. Authorization still checks current database state. Revocation increments the version or changes membership status. There is no per-device session inventory, consistent with removing devices and access_credentials.

room_events is the activity log and reliable replay stream. It is not a second editable task/message database or a full event-sourcing architecture. Event payloads may contain immutable transition summaries and access-check observations; current tasks and messages stay authoritative in their own tables. Live policy and relationships are not hidden in JSONB.

## Transactions and external access checks

### Joining

1. Resolve the connecting companion's selected GitHub identity and verify its required collaborator access to every current project repository. Do not trust a supplied github_user_id alone. Keep external network checks outside long-held database locks.
2. Begin a transaction, lock the project row, and verify that its repository set still matches what was checked; retry verification if the set changed. Repository-association mutations acquire the same project lock.
3. Lock the room, then the invitation in that order. Validate invitation kind, expiry, revocation and use count, human membership, account ownership, room state and available seats. Attaching to an existing user requires that user's valid membership token or linking passphrase; an invitation, display name, account label or claimed provider identity alone cannot prove ownership of an existing user. Count pending_access and active rows. Rejoining an existing reserved account does not consume another seat or invitation use.
4. Create/update membership and participation, consume the invitation if this is a new admission, append events, and commit. Failed capacity/admission checks do not consume an invitation use.
5. Issue the signed session token after commit. A response lost after commit requires a fresh administrator-issued invitation for recovery when no prior credential was received; do not reuse a consumed invitation as an unlimited account-recovery credential. This v1 limitation should be visible in the join flow.

Project mutations, joins, and other operations touching both entities use project-then-room lock ordering; multi-room changes lock rooms in stable ID order. Do not call GitHub while holding these locks.

### Repository changes and dispatch

Adding a repository marks existing reserved participants pending_access transactionally; they cannot receive new tasks until all required access checks pass for the current repository set. Running work stops at a checkpoint before accessing a newly introduced repository. Removing a project-repository association is rejected while an open room has tasks referencing it; close rooms and retain the project rather than deleting its history. Historical tasks in closed rooms retain their repository references even if the project association is later removed.

Admission and work boundaries check current GitHub authorization. Verification events record which identity, repository set, and result were observed, without storing tokens. Revoked permissions or Git failures block the affected work and surface to the admin. The schema cannot guarantee that external permissions will remain valid between checks or govern arbitrary Git commands outside the managed companion.

### Task claims, limits, and messages

- Dispatch locks/checks room eligibility and participant eligibility in the same transaction as updating the task. Optimistic version checks prevent stale task updates. A confirmed usage-limit transition and subsequent dispatch serialize on the room row, so no new assignment is committed after the room enters draining.
- Draining stops new assignments/discussion; running work checkpoints or finishes when possible. An exhausted account cannot be guaranteed to complete an LLM-dependent task. Paused resumes only through the room's authorized policy/admin path.
- Messages/state transitions and their room_events rows commit together. Allocate sequences by updating rooms.last_event_sequence within that transaction. The room row lock orders concurrent commits, preventing a replay cursor from skipping a lower sequence that commits later.
- Publish only committed events. Reconnecting clients request sequence > last_received. Delivery is at least once; clients deduplicate by (room_id, sequence). WebSockets and optional PostgreSQL notifications are wake-up channels, not substitutes for persisted events.
- Kicking changes participant state and token_version transactionally. Revoking a human membership invalidates the corresponding owned participant access too. Preserve discussion and work history.
- Foreign keys restrict deletion of referenced users, accounts, rooms, repositories, tasks and membership records. Routine departure uses status transitions, not DELETE. The only planned cascading delete is usage_windows under an explicitly deleted usage_snapshot, for a future authorized retention policy.
- Bound the room policy and message/event sizes at the API boundary. Network calls and LLM execution never occur inside a database transaction.

## Query and index plan

Start with the indexes supplied by primary/unique constraints plus indexes supporting actual traversal and list queries:

- agent_accounts(user_id); rooms(project_id); project_repositories(repository_id, project_id) for reverse lookup.
- room_memberships(user_id, room_id), plus the active-admin partial unique index.
- participants(account_id, room_id), plus the reserved-lead partial unique index. UQ(room_id, account_id) already supports room seat counting.
- messages(room_id, created_at, id) for bounded timeline pages; messages(room_id, recipient_participant_id, created_at, id) if addressed-message retrieval needs it.
- tasks(room_id, status, updated_at, id), tasks(assigned_participant_id, status), tasks(repository_id), and tasks(parent_task_id) for work queues and references.
- usage_snapshots(account_id, observed_at DESC, id DESC). The usage_windows composite PK supports fetching a snapshot's windows.
- invitations(room_id) for room administration. The unique token_hash index supports redemption.
- room_events already has the required ordered replay index through PK(room_id, sequence).

Composite same-room FKs require UQ(room_id, id) on their target tables even though id is globally unique. Those additional unique indexes are an explicit correctness cost, not accidental duplicate indexes. Foreign keys do not automatically create indexes on their referencing columns; inspect deletion and traversal plans before adding more.

Use keyset pagination, bounded page sizes, short transactions, and a bounded connection pool. Start with one application process and one PostgreSQL instance. Multiple application workers can later share PostgreSQL and use NOTIFY only to wake event readers; readers still fetch authoritative rows using their own cursors. No Redis, sharding, materialized dashboard totals, or table partitioning is required for the initial workload.

Usage history and room_events are the expected growth tables. Initially retain them; expose their size/row growth operationally. Add a documented retention/archive policy before automated deletion. Truncating replay history will require an explicit resynchronization response for clients with old cursors. Do not silently delete events clients still expect to replay.

## Acceptance checks before calling the implementation sound

These are the full target acceptance requirements. The implementation record
identifies the subset executed in Phases 1–3; future features below are not
implied to have passed:

1. users has exactly id and displayname; duplicate display names are allowed.
2. One canonical GitHub repository associates with two projects without duplicate repository metadata; duplicate project associations fail.
3. One user owns two accounts in one room; two seats are consumed and quota snapshots remain separate.
4. Cross-room sender, recipient, parent-task, and assignee references fail. An unassociated repository cannot be assigned to a task.
5. Parallel joins competing for the last seat admit only one; rejected joins do not consume invitation uses. Expired, revoked, and fully used invites fail.
6. Rejoining reuses identity rows, invalidates older tokens, and does not let kicked participants self-admit without admin authorization.
7. Adding a repository blocks fresh dispatch until current participants pass the new access checks. Changed GitHub permissions produce a visible blocked outcome.
8. Concurrent claims have one winner; stale task versions fail. Dispatch racing with draining cannot assign after the draining commit.
9. Two concurrent writes produce ordered, committed room events; rollback leaves neither a message nor its event. Reconnect replay and duplicate delivery produce one visible message.
10. Independent reset windows remain queryable. Unavailable, stale, unknown, and exhausted allowance are distinct, and percentages cannot be invalid.
11. Human/agent authorization, lead/admin transfer, sole-admin departure and token revocation are checked under concurrent requests.
12. On a running PostgreSQL instance, run EXPLAIN (ANALYZE, BUFFERS) for realistic timeline, replay, quota and work-queue queries. Measure application/database latency separately from network and model execution. Define a representative room/history/concurrency workload before claiming capacity.

## Evidence and limits

Revision `0001` and local room behavior have now been executed against PostgreSQL,
including concurrent admission/claims, revocation, replay, and restart recovery.
An end-to-end test traverses three MCP stdio clients and a real HTTP hub into an
isolated PostgreSQL schema. No scalability benchmark or live GitHub permission
validation has been executed. Earlier design details about role transfer and
rejoining describe target behavior, not available endpoints.

PostgreSQL reference documentation used for the design:

- Constraints and cross-row CHECK limitations: https://www.postgresql.org/docs/current/ddl-constraints.html
- Partial unique indexes: https://www.postgresql.org/docs/current/indexes-partial.html
- JSONB: https://www.postgresql.org/docs/current/datatype-json.html

The project has fourteen application tables. Their count is not a scalability problem by itself: the primary concerns are correct relationships, bounded queries, indexes, event/usage growth and transaction contention. Add tables only when a new independent relationship or measurable requirement justifies them.
