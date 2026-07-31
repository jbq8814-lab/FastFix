# FastFix 冻结开发评测聚合

在固定的 FF-003—FF-015 共 13 个开发阶段单次未见合成任务中，11 个产生了通过规定验证的待审批 Candidate：`development_validated_candidate_rate=11/13`（84.6%）。

## 任务集合与口径

- 主集合固定为 FF-003—FF-015；FF-001 与 FF-002 不进入主分母。
- `evaluation_role=development_unseen_baseline`，`metric_eligible=false`，`formal_benchmark=false`。
- 任务为人工构建的 FastAPI 合成缺陷；不是公开 Benchmark、SWE-bench 或生产数据。
- 所有任务均为单次开发运行，失败后没有重跑；Candidate 未 Apply 到 canonical source。

## 主结果

- 分子：11。
- 分母：13。
- 精确值：`11/13 = 0.(846153)`。
- 展示值：`84.6%`。
- Validated Candidate：FF-003, FF-004, FF-005, FF-006, FF-009, FF-010, FF-011, FF-012, FF-013, FF-014, FF-015。

## 每题结果

| 任务 | 分类 | Validated Candidate | 原运行终态 | 后续处置 | targeted | regression | Ruff |
| --- | --- | --- | --- | --- | --- | --- | --- |
| FF-003 | `validated_candidate` | 是 | `approval_pending` | `rejected` | `passed` | `passed` | `passed` |
| FF-004 | `validated_candidate` | 是 | `approval_pending` | `rejected` | `passed` | `passed` | `passed` |
| FF-005 | `validated_candidate` | 是 | `approval_pending` | `rejected` | `passed` | `passed` | `passed` |
| FF-006 | `validated_candidate` | 是 | `approval_pending` | `rejected` | `passed` | `passed` | `passed` |
| FF-007 | `provider_confounded_incomplete` | 否 | `failed` | `none` | `passed` | `incomplete` | `incomplete` |
| FF-008 | `agent_closure_failure` | 否 | `failed` | `none` | `passed_before_rollback` | `passed_before_rollback` | `passed_before_rollback` |
| FF-009 | `validated_candidate` | 是 | `approval_pending` | `rejected` | `passed` | `passed` | `passed` |
| FF-010 | `validated_candidate` | 是 | `approval_pending` | `rejected` | `passed` | `passed` | `passed` |
| FF-011 | `validated_candidate` | 是 | `approval_pending` | `rejected` | `passed` | `passed` | `passed` |
| FF-012 | `validated_candidate` | 是 | `approval_pending` | `rejected` | `passed` | `passed` | `passed` |
| FF-013 | `validated_candidate` | 是 | `approval_pending` | `rejected` | `passed` | `passed` | `passed` |
| FF-014 | `validated_candidate` | 是 | `approval_pending` | `rejected` | `passed` | `passed` | `passed` |
| FF-015 | `validated_candidate` | 是 | `approval_pending` | `rejected` | `passed` | `passed` | `passed` |

## 未产生有效 Candidate

- FF-007：`provider_confounded_incomplete`。Provider HTTP 502 重试耗尽；targeted 已通过，regression 与 Ruff 未完成，没有 approval package。这不构成模型语义修复失败证据。
- FF-008：`agent_closure_failure`。核心 `flush → commit` 修复及三道验证曾通过，但冗余修改清理时 Patch 失败并 rollback，最终 Diff 为空且未提交 Candidate。`metrics-erratum.json` 已应用，历史 `patch_failures=0` 不作为真实累计值。

## FF-009 Reject 与证据限制

- 原 `run-001` 终态为 `approval_pending`；发布前审计随后执行真实 Reject，二者通过 `run_final_state` 与 `post_run_disposition` 分开记录。
- canonical Gold patch 的旧 hunk/末尾换行问题使其无法由当前 `git apply` 直接应用；冻结测试以精确增删行构造临时 Gold 语义状态。
- task metadata 的 Buggy/Gold 计数为 `3/1`、`4/0`；当前冻结执行为 `4/1`、`5/0`。canonical task 与 Gold patch 均未回写；该限制不改变 Candidate 的三道验证通过事实。

## FF-014 语义边界

- 已应用 `assessment-amendment.json`：`fixture_behavior_match=true`，`full_semantic_equivalence=null`。
- Candidate 额外将 handler 从 `async def` 改为 `def`；当前证据不能证明 FastAPI 调度和线程上下文完全等价。

## 次级敏感性分析

排除 Provider-confounded FF-007 后为 `11/12`（91.7%）。这是 secondary、post-hoc sensitivity analysis，不是预设主结果，不得单独作为简历主指标。

## 禁止替换为的夸大表述

- `benchmark pass rate`
- `resolved@1`
- `model success rate`
- `general repair rate`
- `production success rate`

## 来源与完整性

JSON 是唯一权威机器可读来源；本 Markdown 由同一数据模型生成。构建器在必要证据缺失、身份或标签冲突、hash 不一致时失败，并按稳定顺序生成下列 source manifest：

- `benchmarks/results/ff-003-current-baseline/protocol.json` — `49ef5c42f429176147afb13d1fed2213be23f98925ff427f66a8a5d3d594dcd0`
- `benchmarks/results/ff-003-current-baseline/run-001/approval-request.json` — `6d9efd2511227d80436c2f230daf18782ea470202715f8c75ddab533fe5f0167`
- `benchmarks/results/ff-003-current-baseline/run-001/assessment.json` — `41838b313020c68e36074e9ba71b86b3e1c6c1b6b2fab697587ad5b6f8693e31`
- `benchmarks/results/ff-003-current-baseline/run-001/changed-files.txt` — `674b589800fd534f64a2c88412c8a7925cc628c8eabf0fc1e25a2fe3b43bba3f`
- `benchmarks/results/ff-003-current-baseline/run-001/patch.diff` — `24fee7a6c8efc30e7a45c2bc6057b83f05503071d3817519ed123dfd3bf7b373`
- `benchmarks/results/ff-003-current-baseline/run-001/summary.json` — `27717c7b02f4fba84d2279266125a21b45b5a64737b9613dacdb24299b045386`
- `benchmarks/results/ff-003-current-baseline/run-001/validation-summary.json` — `946dd52cca934ab183a6114ddfe589d75f6fba4d1bc847e89534e337dcccefe9`
- `benchmarks/results/ff-004-current-baseline/protocol.json` — `3e400f8da8ae5e1116be4ee85e8931f029a1453c9554b39faf4485fa564dc934`
- `benchmarks/results/ff-004-current-baseline/run-001/approval-request.json` — `f28f0818b600afc42ade151704a2c117f7349e3115cfe6116678474a094ff69f`
- `benchmarks/results/ff-004-current-baseline/run-001/assessment.json` — `bde607e18f541fd6303ea2b334bc482960b8201e0700ccf8845187ecec1ba704`
- `benchmarks/results/ff-004-current-baseline/run-001/changed-files.txt` — `b3b011ae41e939048fa49199e9d358be592982bed2d5d7ce7070dc1b4dae0f2a`
- `benchmarks/results/ff-004-current-baseline/run-001/patch.diff` — `60ac40ca76204d2ecdd700bdebd7fcd35933aa6e6f0a2581a6a4ac4a864ffd6a`
- `benchmarks/results/ff-004-current-baseline/run-001/summary.json` — `acf9be59f9403f83c244bcc54a572fed123a3f6479fbbd162e182f7f08ff8797`
- `benchmarks/results/ff-004-current-baseline/run-001/validation-summary.json` — `fc0aff5d51315236d42c7e2583f398d092fd606f1f8f552241ea1721e24fc518`
- `benchmarks/results/ff-005-current-baseline/protocol.json` — `27687ff028cb7ab9f40119cef66e723c029f5da95e5fcc08d6960962dd870657`
- `benchmarks/results/ff-005-current-baseline/run-001/approval-request.json` — `52aaf5c98c2e29553bc7d5b9e0087d58dd522dc7093dc10f649d61a133305b0e`
- `benchmarks/results/ff-005-current-baseline/run-001/assessment.json` — `a61daa6be5b06ed1fc84bf32cc10b9310a303423305c3488fae3388ccb523bcc`
- `benchmarks/results/ff-005-current-baseline/run-001/changed-files.txt` — `b3b011ae41e939048fa49199e9d358be592982bed2d5d7ce7070dc1b4dae0f2a`
- `benchmarks/results/ff-005-current-baseline/run-001/patch.diff` — `04ab3739c780c8885a007a8a3a5e8cb515dc45b5af8401738aff9ccdd4aa941f`
- `benchmarks/results/ff-005-current-baseline/run-001/summary.json` — `67f159e306c55f2f6bab6905750ad676576714db7c2e041c8b81ce49198366a4`
- `benchmarks/results/ff-005-current-baseline/run-001/validation-summary.json` — `aa93793e368a97d7d0eb59a65378c82f607f243d564aad140a9ecc0dac22472d`
- `benchmarks/results/ff-006-current-baseline/protocol.json` — `2239b7efc2d68f8677841e290c6f2d9a0226d3e4dd68061ff82e63e3489217af`
- `benchmarks/results/ff-006-current-baseline/run-001/approval-request.json` — `b2509b44ae3b335b2bd07995624b615832949070d157b5f98d8665338d71a397`
- `benchmarks/results/ff-006-current-baseline/run-001/assessment.json` — `a12dd8297062292d5201b31b901ef249df20fc93207bd78e9e411874ab1952e3`
- `benchmarks/results/ff-006-current-baseline/run-001/changed-files.txt` — `b3b011ae41e939048fa49199e9d358be592982bed2d5d7ce7070dc1b4dae0f2a`
- `benchmarks/results/ff-006-current-baseline/run-001/patch.diff` — `b185b1de661be4b719c2a9083572372f526c7b5e9d398c109f853896ec38e7c4`
- `benchmarks/results/ff-006-current-baseline/run-001/summary.json` — `36444296dab01102e250e3f92326dcdb30ce1076c8929b4a7f7d043fd496dcdd`
- `benchmarks/results/ff-006-current-baseline/run-001/validation-summary.json` — `a7e80cb400152336ca9e2ea5203b0d8fabcb255d922748dd6d41ccd2458a897e`
- `benchmarks/results/ff-007-current-baseline/protocol.json` — `a991dbd25cf565c48970aabf7897d8f2ba94bfdcc5697a98cb8cef82d7a90f81`
- `benchmarks/results/ff-007-current-baseline/run-001/assessment.json` — `4cb8b6efec18092827c2a5a08df986848adfd6a9c3f6efc35ae96df028d3d968`
- `benchmarks/results/ff-007-current-baseline/run-001/changed-files.txt` — `4bc0b3b6cef54b770c59885477abf0bf9494dcff1c40d923523e59b784306e3b`
- `benchmarks/results/ff-007-current-baseline/run-001/failure.json` — `97e9ce01b63f5c7c203ba9274d063ec70d162883c724fec2785498e61c14876f`
- `benchmarks/results/ff-007-current-baseline/run-001/summary.json` — `e92029029b29814f9e8343f48aacccc6a8e2ad2e1af85c7ecb5e96962d567815`
- `benchmarks/results/ff-007-current-baseline/run-001/tool-calls.json` — `ccc07dfee1ba0b97e916e5e722f1a1e7b8c9472a5dd76a02d22302190b9c3e7b`
- `benchmarks/results/ff-007-current-baseline/run-001/validation-summary.json` — `e839ff9d8b262d0028b9a5dea080cb0bca664b4fab92f09ce13c7efbce1ac63c`
- `benchmarks/results/ff-008-current-baseline/protocol.json` — `1f3beaf1793919d86a953846096784b0bb60a565d5ea1f8531e722324676747c`
- `benchmarks/results/ff-008-current-baseline/run-001/assessment.json` — `55096bd3d9d95dd1f6b8978db3919898cf0a54d632ae9d8ed9504dd9320eda30`
- `benchmarks/results/ff-008-current-baseline/run-001/changed-files.txt` — `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`
- `benchmarks/results/ff-008-current-baseline/run-001/failure.json` — `b24bab0d4f51e68e60ca8f95548f893aca000ec737d10063dd0e55a608bca2d2`
- `benchmarks/results/ff-008-current-baseline/run-001/metrics-erratum.json` — `4822adcc654812940be9a248c6921729a9b7cdbf777cfaac044d544921513de2`
- `benchmarks/results/ff-008-current-baseline/run-001/summary.json` — `d71df7e4b438098702d9ea3b2c200d01404829cb730f2a107642b85c9ff313a0`
- `benchmarks/results/ff-008-current-baseline/run-001/tool-calls.json` — `71b60377b9a9692dd289fe77ed15c712e5376f9bbac38a9834bb9e36fe131480`
- `benchmarks/results/ff-008-current-baseline/run-001/validation-summary.json` — `e8502086e866d16c452aa264dcf770e45d7070986c35221070834d3936aedd88`
- `benchmarks/results/ff-009-current-baseline/protocol.json` — `2e3de2e0f42c5f23472328b70a565014b3cb56808c47ee85b3100169379bcd29`
- `benchmarks/results/ff-009-current-baseline/run-001/approval-package-manifest.json` — `75898ad83aeaaac98c213f23649259202778b5cf92c20415abec443d86fa3fac`
- `benchmarks/results/ff-009-current-baseline/run-001/approval-request.json` — `7243338ce5ab559c9ba894e68814a11138422c70259ed346205c7d95734026cd`
- `benchmarks/results/ff-009-current-baseline/run-001/assessment.json` — `5d2d5ed12876144ccc9d7b2d2205e5df8b45c50a292f17fb1e6fa6856e025445`
- `benchmarks/results/ff-009-current-baseline/run-001/changed-files.txt` — `70778f7428a7dc2c642a9fa94bc6876bd0047e9613181c22e6a1d235bf8b9816`
- `benchmarks/results/ff-009-current-baseline/run-001/patch.diff` — `4bee50abf5eddac81332ae5ec1f7120d64f02926a2f43b4d834b8b38d37ea0ae`
- `benchmarks/results/ff-009-current-baseline/run-001/reject-audit.json` — `738480a2dd2c0ebdc2a298aaf315e7280389fb464a0b466b71157f29ad74ae06`
- `benchmarks/results/ff-009-current-baseline/run-001/reject-decision.json` — `b2ca1c8b95d6f69e538c2eab971f2194abf2eb4cd441cbaaf3912c948afa6b58`
- `benchmarks/results/ff-009-current-baseline/run-001/summary.json` — `9c2d9301a98f76f0328eb41b22080513637da932fe6c50564afbb7546b1f099d`
- `benchmarks/results/ff-009-current-baseline/run-001/validation-summary.json` — `42defc11664434c7b1b05099328f9a3827546e7bed9b0350f32e5678f759c23f`
- `benchmarks/results/ff-010-current-baseline/protocol.json` — `a081d2e561381b17996ca925d28566f7f188be78ba7753bd5b1cbe79f47005a8`
- `benchmarks/results/ff-010-current-baseline/run-001/approval-request.json` — `4be8dc1629b57195245b02bf112e9e84331c596f5c3b9d13941562680194fa02`
- `benchmarks/results/ff-010-current-baseline/run-001/assessment.json` — `d4125fb91d0853543df64d2614faaed12d991a40298826bc33d55138e40b7cbf`
- `benchmarks/results/ff-010-current-baseline/run-001/changed-files.txt` — `62765825622e78846160104e0c2576ba60ec81f853ba23f07dddfcdacc3916c4`
- `benchmarks/results/ff-010-current-baseline/run-001/patch.diff` — `eedcfafb1a47c5c2e3cfa0961ae30df7c734d93581612029327ba0b360afd71f`
- `benchmarks/results/ff-010-current-baseline/run-001/summary.json` — `ea1cad554f8ed10f3b8c7b4e225df36a1387954c1282bfa9e4a4499ed530f8fd`
- `benchmarks/results/ff-010-current-baseline/run-001/validation-summary.json` — `5a927c21ea1e26f4c9e3008d164369442bd0e3983701a17bca3a8a7dd20e89ac`
- `benchmarks/results/ff-011-current-baseline/protocol.json` — `b4813bedf15c604c5a422ab9e1c2c84bd85474f3c3572833c90c33587cc7782c`
- `benchmarks/results/ff-011-current-baseline/run-001/approval-request.json` — `ed93d622f8c1ce3ef4fd43fd1fec63a399883144afe4eda5e4ed9aa12e47486b`
- `benchmarks/results/ff-011-current-baseline/run-001/assessment.json` — `659035af5e8d870156a7263981da76f17983ec7c8726068531b1581e4bd22d64`
- `benchmarks/results/ff-011-current-baseline/run-001/changed-files.txt` — `b3b011ae41e939048fa49199e9d358be592982bed2d5d7ce7070dc1b4dae0f2a`
- `benchmarks/results/ff-011-current-baseline/run-001/patch.diff` — `b197080923d94450252be2e8740ffd60f52b7f68d22a495c82f283d025dfbc5b`
- `benchmarks/results/ff-011-current-baseline/run-001/summary.json` — `13ea6052a5e031aab9deb826fd2cfd711fca6f0733d8dc51879938c14e7d76b6`
- `benchmarks/results/ff-011-current-baseline/run-001/validation-summary.json` — `83e5b5f2cc24ceb4c546c35aae1b02ccaf86454a5eac380ec12aa1515df1a978`
- `benchmarks/results/ff-012-current-baseline/protocol.json` — `1e934e875586f5467d5c7919820aa44ae2614c78790829c3d094ba8eaf5ce64d`
- `benchmarks/results/ff-012-current-baseline/run-001/approval-request.json` — `f5266cddf9e75b5673ad2a394a7c13a679d08bc32f7848c2baaafca3801354e7`
- `benchmarks/results/ff-012-current-baseline/run-001/assessment.json` — `e895e5ba400ef6742f5b1c62fbc3061c7e5810ed87e0eb5c3e45d56a86a0cf22`
- `benchmarks/results/ff-012-current-baseline/run-001/changed-files.txt` — `674b589800fd534f64a2c88412c8a7925cc628c8eabf0fc1e25a2fe3b43bba3f`
- `benchmarks/results/ff-012-current-baseline/run-001/patch.diff` — `8984f7c9464bfee4cd3a0aad3514a71f1d692119201981c59f6f35a093015001`
- `benchmarks/results/ff-012-current-baseline/run-001/summary.json` — `697506423656fca200160be92e1bf1ba7bd983d770a014d4f8b198e1b38c3cbe`
- `benchmarks/results/ff-012-current-baseline/run-001/validation-summary.json` — `5a9ffc29522ddbce54820cc6ebab24e1be79bcab92057272653cbb8695129053`
- `benchmarks/results/ff-013-current-baseline/protocol.json` — `93b8d8282bdeeb2e03b3b51afd3803b0f39d1d97a40702ecdb48ba79f2cf7c05`
- `benchmarks/results/ff-013-current-baseline/run-001/approval-request.json` — `eb6a69104df17e17d18eb21821801d41833a2533fb8c66678ee4f188d1f60dff`
- `benchmarks/results/ff-013-current-baseline/run-001/assessment.json` — `717085805b787ba4498aa9a8215f05b317c1fd2488e08959a2627e7abcf643ce`
- `benchmarks/results/ff-013-current-baseline/run-001/changed-files.txt` — `ab6facb2f704724e779fdbf780fe90d2f78f0a8dcf853b9ea68d207f5f5ca41a`
- `benchmarks/results/ff-013-current-baseline/run-001/patch.diff` — `6f796548b8eb9fc6ac1e95c75ee023fe35216e4aa8c216063d840c78d2aee6a5`
- `benchmarks/results/ff-013-current-baseline/run-001/summary.json` — `2808a21644166e6206b6346e34fcdb6dc1e1ebabd1ba46ea5f9091901d3a16f1`
- `benchmarks/results/ff-013-current-baseline/run-001/validation-summary.json` — `070070bd65fca0c64efd49d4ee69713c5d1b931767840034cd682f2ea24ea4da`
- `benchmarks/results/ff-014-current-baseline/protocol.json` — `62c30dfe0d97cb40a27c81bdb9322f6cb373c0c6750d62dd40f5120ad7472849`
- `benchmarks/results/ff-014-current-baseline/run-001/approval-request.json` — `2c4b23d5302ae82884da8aacbacfe103bb1fb9f381e35aa2ea68a3345832e0b3`
- `benchmarks/results/ff-014-current-baseline/run-001/assessment-amendment.json` — `6642882bc34220875fd5cb14be4a3367bfea95f16bb53ba56755e4d2b9a14127`
- `benchmarks/results/ff-014-current-baseline/run-001/assessment.json` — `9ec5f148fa395bbcea8b549631c57ae8fd9a17dc7df8092b2324a2c87b6ce133`
- `benchmarks/results/ff-014-current-baseline/run-001/changed-files.txt` — `b3b011ae41e939048fa49199e9d358be592982bed2d5d7ce7070dc1b4dae0f2a`
- `benchmarks/results/ff-014-current-baseline/run-001/patch.diff` — `cd00c5572938af493b4406944f9c7a2c4227387472f333f891e6dc140591dd1c`
- `benchmarks/results/ff-014-current-baseline/run-001/summary.json` — `d99e7ce858da3b1dd35f00861acb8c952f61a07da10a9a8c3183cbd5882026c1`
- `benchmarks/results/ff-014-current-baseline/run-001/validation-summary.json` — `e0a6da3dd75878847a343a6eb75339af22900a8c70a34567a557df5bf0eb2f62`
- `benchmarks/results/ff-015-current-baseline/protocol.json` — `af63be5a28fad043ee48145b43aafe73a42ce372316d28ff0bf766d5bae4f6de`
- `benchmarks/results/ff-015-current-baseline/run-001/approval-request.json` — `34238017f3cfd70d43b34100148ecc15604c7873b8c071419b1ff36809643628`
- `benchmarks/results/ff-015-current-baseline/run-001/assessment.json` — `77dad9924713f92c7844ec686d0091a8546fe21818a6eaf464f47f1eaaf86e72`
- `benchmarks/results/ff-015-current-baseline/run-001/changed-files.txt` — `d38a4e8bf723b38ca854278eea7b04ab0cd26b608c16340ce45a0fb4db8dbe90`
- `benchmarks/results/ff-015-current-baseline/run-001/patch.diff` — `f82ee8fae0d6dec4e4d9fc4b2e6c0bd3d8b8e86b8730219bbfa9d3c93e8d0381`
- `benchmarks/results/ff-015-current-baseline/run-001/summary.json` — `bf9957c5acbb67520e2ce8bf6e48c1cabd99e74318b12eb264a8b9f189901572`
- `benchmarks/results/ff-015-current-baseline/run-001/validation-summary.json` — `bc3e2cebb38a6c06a0350b52f9538355dbd7328d98323b0027a3edfd46438c3b`
- `benchmarks/tasks/ff-009-session-lifecycle/gold.patch` — `9c5085a0a01b52045f120bf3d6cca600f38f104afb17d1aa6c5a394dc91fb584`
- `benchmarks/tasks/ff-009-session-lifecycle/task.json` — `fb9d15383bb514c28d54d094bbf771f5e4e61eba0395da2b7249cf613cccfe39`
- `benchmarks/tasks/ff-014-awaiting-sync-service/gold.patch` — `640bd9b9cc05af5aafcf76b1991fa5240be19fd0765d025dd911931fe0f597e9`
- `tests/fastfix/test_ff009_fixture.py` — `ee4075a66ef2c7922def047be6c332123bd04aef131a1b41998265f3dd71ca81`

Frozen source commit：`ae9a7ece3e0dac43b93054cdc62a11fa94e244fc`。
