# Garmin AI Rust migration progress

The compatibility baseline is `ee3a14a48a6ad30c1085951bac2f53c9088c2450`. It
contains the Gemini diagnostics change that merged after the handoff's SHA.

## Verified release checkpoint

The [checkpoint](evidence/native-release-checkpoint-44617c2.json) records the
synthetic checks of `44617c219d262292c7487a37ee6794a9e4c8bc7e` on main.
It binds the tested Git tree, the workflow runs, the application image and
installed platform digests, and the database schema, parser and backup
versions to their receipts.
The recorded images came from disposable test registries.

- R34–R35. Five physical AMD64 and ARM64 installs verified the packaged
  runtime, including historical upgrades and the AMD64 GCP profile. The
  installations verified the encrypted backup, portable export and native
  recovery. The checkpoint includes the installed license archives' hashes.
- R36. The inventory covers 459 surfaces across 25 areas. The strict check has
  nine open areas at this checkpoint. The owner approved the four documented
  differences on 2026-11-22, leaving five open in the current inventory.
  The Python-based CI suite passed 4,385 cases and recorded
  80 skips. A separate CI step passed the same 54 skipped extension cases.
  The checkpoint records the remaining filesystem and live model-provider skips.
- R38–R39. Four recoveries on independent Linux hosts verified 30 tables and
  three files per case. The Rust-to-Rust run preserved the acknowledged diary
  edit, completed the restored job once and saved the next revision. The
  tested native installations used a Python-free API and worker.

The checkpoint identifies the exact workflow definition and tested revision
for each run. The Rust CI run belongs to the preceding commit. The source
changes between those two commits are the installer guard and its tests.
Run the inventory and strict checks at the checkpoint revision with
`python3 migration/check_parity.py` and
`python3 migration/check_parity.py --require-verified`. Release acceptance,
the 168-hour observation, final rehearsal and production cutover remain open.
See the [open verification conditions](blockers.md).

## Implementation snapshot

The implementation snapshot below follows `6342db821a20fc80374a7cdfb10036c30780a453`
on main. Test evidence is scoped to the revision, environment and workload
that produced it.

| Task | Evidence on main | Remaining work |
| --- | --- | --- |
| R00 | The pinned Python 3.15.2 run collected 4,369 cases. It passed 4,342 and skipped 27. The full run includes the previously separate synthetic extension cases. | Complete the explicitly authorized live comparisons. |
| R01 | The inventory assigns all 459 interfaces and contracts across 25 areas. The strict parity check reports every pending area. | Complete the comparisons and release decisions recorded by the strict gate. |
| R02–R04 | Matched cold startup, restart, encrypted backup and restored content on an isolated 2-CPU, 1 GiB Linux guest. Separate cgroup tests covered heavy replay, recorded the database reaching its cap, and kept native control requests responsive. [Resource checkpoints](evidence/native-resource-checkpoints-linux-arm64.json) | Repeat on the final candidate and complete the sustained observation. |
| R05–R08 | Linux native workspace and health checks, domain identity and precise JSON, schema-aware database readiness, immutable archive, verified reads and a read-only mount. #221 #223 #224 #225 #226 #227 #267 #273 | Integrate these boundaries across the application. Verify storage lifecycle with restored databases and backups. |
| R09 | Durable priority jobs, separate endpoint capture and publication, and lease renewal through live fetch and staging. A one-second starting lease survived slower fetch and publication. Failed publication restored the earlier deadline. #232 #236 #245 #292 #299 #303 | Compose and supervise all workers, then exercise service restarts and producer delays. |
| R10–R12 | Garmin capability matrix, Personal API credentials, native interactive login and enrollment, and all 29 legacy read requests. The signed Telegram Mini App reauth and worker notice preserve account ownership, token publication and durable pauses. The worker rediscovers refreshed credentials and verifies the account before fetching. #229 #233 #237 #239 #242 #251 #303 #458 #461 #465 #472 #476 | Exercise the authorized account, device, rate-limit and authentication matrix. |
| R13 | Immutable raw JSON, activity documents and summaries, daily and intraday publication, and attested intervals. The normal worker schedules the native calendar and activity jobs using the recorded owner timezone. Endpoint capture and publication preserve the owner, accepted source and previous freshness on failure. #241 #248 #253 #255 #262 #281 #292 #299 #461 | Verify restart recovery and freshness during the representative combined workload. |
| R14–R15 | FIT decoder comparison, raw fields, ZIP and bounded staging. Native projection matched 50,047 records from 15 synthetic inputs and two known errors. A 100 MiB staged run fit a 32 MiB container. The scheduled activity and FIT jobs preserve publication and queue state across retry. #230 #234 #243 #247 #249 #257 #266 #272 #277 #456 #457 #461 | Verify the full release workload and the previously archived FIT corpus. |
| R16 | Verified calendar, intraday, activity-part, activity-summary and original FIT/ZIP reconstruction. Replay protects retained ownership and restores changed summaries with their insight invalidation. History status matched 7,300 windows. The combined dispatcher passed all five archive route families and the periodic planner matched up to 833 scheduled jobs. Archive replay and acquisition are both served by the normal worker. #254 #260 #271 #275 #276 #279 #282 #284 #291 #294 #300 #307 #311 #313 #318 #319 #324 #461 | Exercise restart, backlog drain and rollback against the combined workload. |
| R17 | System diary creation, exact validation, versioned evidence, audited corrections, group undo and delete. Native Telegram and web chat actions use the same mutation boundary. A low-confidence undo asks for clarification and a stale web command cannot undo a newer diary write. #235 #240 #244 #250 #258 #265 #298 #536 #543 | Repeat the combined comparisons against representative upgraded diary histories. |
| R18 | Versioned definitions and metrics, exact numeric bounds, native registration and retirement, and audited custom entries. The HTTP catalog matches scopes, cursors and old versions. Native HTTP mutations preserve creation receipts, revision conflicts, activation and retirement. #246 #252 #256 #268 #278 #287 #290 #421 #430 #506 #547 #551 | Rerun the definition, generated form and restore comparisons against the assembled release. |
| R19 | Native generated forms, local selections, tracker setup, community pack installation, and tracker settings. The selected Telegram English and Russian tracker prompts retain provenance, context and delivery receipts through paired replay. Native scenario-pack endpoints preserve defaults, revision conflicts and separate tracking and sharing permissions. #261 #264 #482 #483 #485 #492 #494 #497 #520 #521 #522 #523 #524 #525 #526 #545 | Complete saved onboarding and all remaining tracker and voice comparisons in the combined release. |
| R20 | Native Source v1 polling resumes an interrupted page. Source-to-metric mappings and CSV/JSON file preview and apply match frozen confirmations, exact typed values, cross-runtime repeats and whole-file rollback. Deeper JSON is admitted only within verified Linux ARM64 stack bounds. #263 #269 #274 #280 #283 #439 #440 #474 #486 #488 #496 #528 | Complete combined source correction, deletion and replay acceptance, and verify nesting on the second platform. |
| R21–R22 | Native timing-only sleep, replay-aware analytical reads, source freshness, timeline and FIT device history. Paired fixtures cover period and lag comparison, event and migraine windows, running efficiency, stress hours, wellbeing, sleep and coffee, temporal display, and generic entry and observation history, aggregation, completeness and thresholds. Native calendar import retains explicit plan evidence, restricts collection to selected calendars, and preserves prior data when a fetch fails. #259 #270 #271 #288 #289 #295 #296 #297 #298 #310 #312 #322 #493 #498 #499 #500 #501 #502 #503 #504 #505 #508 #509 #510 #511 #512 #513 #514 #515 #516 #517 #518 #519 #549 | Complete evidence revalidation in the ordinary channel flows. Repeat numerical and worst-range resource comparisons for the candidate. |
| R23 | Scoped HTTP and MCP authentication, exact definition-version and recipient consent, and send-time revalidation in ordinary workers. Older consent is retained, and diary paging validates permissions before emitting a row. Model consent changes during requests are tested on synthetic providers. #286 #301 #304 #315 #332 #541 #548 | Verify the combined onboarding, plugin, and channel privacy cases against upgraded state. |
| R24 | Native Gemini, OpenAI and Anthropic configuration, consent and timeout/fallback handling through normal web chat and Telegram. The selected model extension uses the same policy and bounded request path, including durable provider cooldown and suppression when consent changes mid-response. #302 #306 #309 #314 #317 #320 #321 #323 #325 #327 #328 #333 #334 #338 #491 #527 #529 #534 #537 #541 | Run authorized provider checks and the combined test through all supported channels and workloads. |
| R25 | Ordinary native web and Telegram workers support scoped dialogue, corrections, undo, and bounded model answers grounded in allowed diary events. The web comparison retained memory after one delivery receipt and hid stale unread answers in Rust and the Python rollback reader. The native web history also rechecks historical single-night sleep timing before display and read acknowledgment. #347 #352 #353 #355 #360 #361 #448 #449 #451 #453 #454 #455 #459 #481 #484 #487 #489 #491 #529 #536 #543 #548 #553 | Complete broader health tools, timing and dependent evidence, historical continuation comparisons, and full parity for analytical answers. |
| R26 | Durable neutral inbox, one-use actions, delivery receipts, reply claims, explicit deferral and no retransmission after uncertain network delivery. The normal worker delivers through explicitly selected channel extensions. #329 #330 #331 #335 #337 #340 #348 #358 #362 #533 #538 | Rehearse pending legacy work ownership, transfer and drain with the packaged release. |
| R27 | Native owner-authorized webhook and polling, acknowledgement lane, durable cursors, diary controls, private histories, and Garmin reauthorization notices. Voice ingress validates the provider envelope and audio bounds, recovers confirmed synthetic transcripts and honors audio consent. #336 #339 #341 #342 #343 #344 #345 #346 #349 #350 #354 #356 #357 #410 #419 #424 #425 #433 #464 #468 #469 #475 #476 #478 #480 #527 #534 | Complete native owner pairing, the combined caption and tracker voice matrix, and the approved private-bot check. |
| R28 | Native HTTP/operations, scoped tools, dashboard assets and local demo. Chromium verified web history, visible delivery receipts, due tracker check-ins, and retry after a lost submission. Web model requests now cover diary actions and bounded evidence-based answers. #293 #308 #316 #322 #332 #463 #467 #471 #473 #477 #485 #529 #536 #543 #544 #545 #546 #547 #548 #553 | Finish the remaining route and compound history comparisons and run the full browser and dashboard matrix. |
| R29 | Native MCP stdio provides read tools, resource listing and templates, opt-in audited diary and custom-entry writes, and explicit cancellation. Wearable receipts retain owner confirmation and the original write-only scope. #408 #412 #413 #422 #426 #429 #434 #435 #436 #506 #507 #508 #509 #530 #531 | Run the full MCP and wearable flow matrix on the assembled release, including after-commit recovery. |
| R30 | Daily budget, quiet hours, diary and context questions, Telegram checks before delivery, and native tracker check-ins with selected fallback. Native web check-ins appear during history polling, accept a browser read receipt, and cancel when owner policy changes before exposure. #437 #438 #441 #442 #443 #444 #445 #446 #532 #542 #544 | Complete trend generation and delivery, and repeat shared budgets, failure windows and repeat suppression under normal worker load. |
| R31 | Versioned source, channel and structured model subprocess protocols use explicit owner selection. Native examples exercised the normal pipeline. Late erasure, malformed responses, unavailable providers and revocation are covered in focused synthetic comparisons. #439 #440 #474 #533 #535 #537 #538 #541 | Verify the complete deployed extension inventory and approved third-party compatibility path. Re-run capability checks under the release configuration. |
| R32 | Native encrypted backup, database restore and verified extraction, storage-space estimates and checks, scheduled backup and retention, and primary-channel capacity warnings. Python-compatible receipts prevent repeat sends, and explicit reconciliation was tested after uncertain delivery. #384 #386 #388 #391 #393 #396 #397 #404 #460 #462 #466 #470 | Run the full restore, disk failure and versioned rollback rehearsal against the release candidate. |
| R33 | Native retention and erase/resume preserve lifecycle and separate backups. Cross-process checks include active workers, later source pages, scoped API and MCP writes, re-enrollment, and recovery after interruption. #392 #394 #402 #403 #417 #535 | Repeat the complete writer and recovery matrix on the final release package. |
| R34 | The source-free native package passed clean installation and historical upgrade on physical AMD64 and ARM64 runners. Standard installation also passed the synthetic AMD64 GCP test. [Installation evidence](evidence/native-install-recovery-linux.json) | Repeat the release matrix for the final accepted source tree. |
| R35 | The native schema chain verified supported historical revisions and a clean install. Physical AMD64 and ARM64 packages passed historical upgrade, and independent recovery hosts restored both backup formats. [Installation evidence](evidence/native-install-recovery-linux.json) | Repeat the candidate upgrade and recovery checks together. |
| R36 | The pinned reference, contract inventory, and focused paired checks cover the local synthetic scope recorded in the evidence. | Require every mandatory parity, user flow, and fault scenario to pass. Complete authorized account, channel, and provider checks. |
| R37 | The short Linux host comparison and capped replay record resource peaks, capped database events, and response times. The observer records actual samples and gaps. [Resource checkpoints](evidence/native-resource-checkpoints-linux-arm64.json), [observer](rust-soak-observer.md) | After R36, observe the stable final candidate unattended for 168 hours. |
| R38 | Synthetic worker handover and rollback retained pending work, prior and new writes, and confirmed or uncertain receipts. Separate Linux hosts restored 30 tables and three files in both directions. [Recovery evidence](evidence/native-install-recovery-linux.json) | Run the final rehearsal after the acceptance gates and measure recovery in the authorized target class. |
| R39 | The packaged standard API and worker ran as native processes, and installation, upgrade and recovery succeeded without Python in those runtime containers. [Installation evidence](evidence/native-install-recovery-linux.json) | Close R36 through R38 before completing the Python exit gate. |
| R40 | The cutover and rollback procedures are ready for rehearsal. [Cutover procedure](cutover-runbook.md) | Obtain authorization for the named production installation, then complete and record the release. |

Local tests, CI, review and release acceptance are separate evidence. See
[open verification conditions](blockers.md) and the
[compatibility register](deviations.md).
