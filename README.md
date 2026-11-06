# Repository Coverage

[Full report](https://htmlpreview.github.io/?https://github.com/uberblick-ai/ub-agents/blob/python-coverage-comment-action-data/htmlcov/index.html)

| Name                                             |    Stmts |     Miss |   Cover |   Missing |
|------------------------------------------------- | -------: | -------: | ------: | --------: |
| src/ub\_agents/\_\_init\_\_.py                   |        1 |        0 |    100% |           |
| src/ub\_agents/\_\_main\_\_.py                   |        2 |        0 |    100% |           |
| src/ub\_agents/agent\_health.py                  |       57 |        0 |    100% |           |
| src/ub\_agents/approvals.py                      |      345 |       11 |     97% |167, 178, 191, 204, 217, 226, 287, 303, 392, 443, 455 |
| src/ub\_agents/assignment\_run.py                |      284 |        6 |     98% |80-81, 180-183 |
| src/ub\_agents/attention.py                      |       80 |        0 |    100% |           |
| src/ub\_agents/bots.py                           |        7 |        0 |    100% |           |
| src/ub\_agents/checkout\_setup.py                |      138 |        7 |     95% |21, 35, 61, 95, 99, 103, 123 |
| src/ub\_agents/cleanup.py                        |      309 |       12 |     96% |43, 123, 128, 209, 239, 264-265, 269, 299, 329, 367-368 |
| src/ub\_agents/cleanup\_control.py               |       59 |        0 |    100% |           |
| src/ub\_agents/cli/\_\_init\_\_.py               |       93 |        0 |    100% |           |
| src/ub\_agents/cli/\_registry.py                 |       23 |        0 |    100% |           |
| src/ub\_agents/cli/\_runtime.py                  |       29 |        0 |    100% |           |
| src/ub\_agents/cli/doctor.py                     |       26 |        0 |    100% |           |
| src/ub\_agents/cli/init.py                       |       27 |        0 |    100% |           |
| src/ub\_agents/cli/launch.py                     |      201 |        8 |     96% |136, 212, 216-218, 245-248 |
| src/ub\_agents/cli/monitor.py                    |       41 |        3 |     93% |     13-15 |
| src/ub\_agents/cli/read.py                       |       17 |        0 |    100% |           |
| src/ub\_agents/cli/report.py                     |       40 |        1 |     98% |        37 |
| src/ub\_agents/cli/retrospective.py              |       17 |        0 |    100% |           |
| src/ub\_agents/cli/retry.py                      |       61 |        0 |    100% |           |
| src/ub\_agents/cli/status.py                     |      117 |        0 |    100% |           |
| src/ub\_agents/config.py                         |      487 |        9 |     98% |98, 309, 338, 361, 483, 494, 523, 572, 589 |
| src/ub\_agents/coordination.py                   |      640 |       14 |     98% |119-120, 154, 162-163, 336, 370, 419, 435, 617, 718, 751, 763, 804 |
| src/ub\_agents/denials.py                        |       47 |        0 |    100% |           |
| src/ub\_agents/dependencies.py                   |       31 |        0 |    100% |           |
| src/ub\_agents/diagnostic\_retention.py          |      197 |       13 |     93% |57, 70, 89, 108, 120, 138, 164-165, 174, 185, 187, 211, 224 |
| src/ub\_agents/discovery.py                      |      225 |       15 |     93% |49-58, 225, 245, 249, 263, 268 |
| src/ub\_agents/doctor/\_\_init\_\_.py            |        1 |        0 |    100% |           |
| src/ub\_agents/doctor/doctor.py                  |      482 |        3 |     99% |332, 495, 522 |
| src/ub\_agents/doctor/doctor\_mcp.py             |      232 |       15 |     94% |51, 58-59, 71, 89, 123, 228, 245, 257-258, 271, 274, 283, 289-290 |
| src/ub\_agents/doctor/doctor\_output.py          |      153 |        4 |     97% |29, 50, 79, 84 |
| src/ub\_agents/doctor/doctor\_progress.py        |       82 |        3 |     96% |     46-48 |
| src/ub\_agents/doctor/mcp\_probe.py              |       95 |       14 |     85% |34, 37-38, 44, 53, 61, 65, 80-81, 95, 104, 110, 114-115 |
| src/ub\_agents/doctor/registry.py                |       17 |        0 |    100% |           |
| src/ub\_agents/eligibility.py                    |       35 |        0 |    100% |           |
| src/ub\_agents/errors.py                         |       28 |        0 |    100% |           |
| src/ub\_agents/events.py                         |      112 |        1 |     99% |        59 |
| src/ub\_agents/execution.py                      |      293 |        8 |     97% |42, 151, 183, 300, 303-304, 317, 335 |
| src/ub\_agents/finalization.py                   |       48 |        0 |    100% |           |
| src/ub\_agents/github.py                         |      796 |       42 |     95% |46, 100, 270, 311, 386, 547, 578, 596, 653, 657, 685, 706, 757, 764, 770, 781, 790, 828, 837, 843, 874, 877-878, 881, 910, 945, 947-948, 996, 1003, 1009-1012, 1058, 1065-1066, 1068, 1091, 1098, 1100-1101 |
| src/ub\_agents/github\_http.py                   |      115 |        5 |     96% |77, 91-92, 157-158 |
| src/ub\_agents/help.py                           |      136 |        0 |    100% |           |
| src/ub\_agents/helper\_command.py                |       10 |       10 |      0% |      3-15 |
| src/ub\_agents/hooks.py                          |       74 |        1 |     99% |        34 |
| src/ub\_agents/init/\_\_init\_\_.py              |        0 |        0 |    100% |           |
| src/ub\_agents/init/boards.py                    |       96 |       10 |     90% |51, 53, 56, 58, 61, 68, 99, 107, 110-111 |
| src/ub\_agents/init/checks.py                    |       33 |        1 |     97% |        25 |
| src/ub\_agents/init/edit.py                      |       91 |        7 |     92% |37, 40, 51, 54, 62, 93, 113 |
| src/ub\_agents/init/extend.py                    |      174 |        9 |     95% |31, 35-36, 41, 110, 143, 166, 180-181 |
| src/ub\_agents/init/flow.py                      |      423 |        9 |     98% |47, 68, 70, 127-128, 215, 312-313, 502 |
| src/ub\_agents/init/inline.py                    |       92 |        2 |     98% |   92, 103 |
| src/ub\_agents/init/mcp.py                       |      129 |       10 |     92% |28, 90, 96-97, 125-127, 155, 166-167 |
| src/ub\_agents/init/probes.py                    |      185 |       18 |     90% |68-69, 84-86, 89, 91, 126, 130-131, 140, 161, 171-172, 177, 179, 186-187 |
| src/ub\_agents/init/prompts.py                   |      152 |        6 |     96% |120, 158, 168, 176, 192, 209 |
| src/ub\_agents/init/setup.py                     |      272 |       21 |     92% |34-35, 40-41, 50, 95, 101, 104, 111, 119, 150, 154, 216, 224, 258-260, 265, 278, 327, 330 |
| src/ub\_agents/init/tailoring.py                 |        5 |        0 |    100% |           |
| src/ub\_agents/keep\_awake.py                    |       70 |        5 |     93% |66, 70-71, 77-78 |
| src/ub\_agents/labels.py                         |       67 |        0 |    100% |           |
| src/ub\_agents/launch\_ui.py                     |      153 |       23 |     85% |36, 63-64, 90-95, 106-107, 155-156, 167-168, 171-177, 181 |
| src/ub\_agents/launcher\_code.py                 |      106 |       19 |     82% |55-66, 100-107 |
| src/ub\_agents/launcher\_lock.py                 |       88 |        6 |     93% |20-22, 35, 74, 95 |
| src/ub\_agents/logs/\_\_init\_\_.py              |        0 |        0 |    100% |           |
| src/ub\_agents/logs/launch\_log.py               |      105 |        3 |     97% | 18, 69-70 |
| src/ub\_agents/logs/log\_format.py               |      458 |       15 |     97% |117, 149-150, 228-229, 246-247, 299-300, 336, 376, 379, 485, 510, 514 |
| src/ub\_agents/logs/log\_json.py                 |      144 |        2 |     99% |  153, 167 |
| src/ub\_agents/logs/log\_limits.py               |        3 |        0 |    100% |           |
| src/ub\_agents/logs/log\_reader.py               |      212 |        3 |     99% |92-93, 142 |
| src/ub\_agents/logs/log\_times.py                |      115 |        4 |     97% |35, 50-51, 123 |
| src/ub\_agents/loop.py                           |      911 |       34 |     96% |259, 289, 349, 359, 368, 435-437, 565, 571, 582, 652, 731, 764, 787-788, 795, 807, 844, 851, 860, 913-914, 931, 966-969, 976, 1011-1012, 1020, 1039, 1095 |
| src/ub\_agents/merging.py                        |      135 |        7 |     95% |54, 72, 74, 130, 154, 160, 165 |
| src/ub\_agents/monitor/\_\_init\_\_.py           |       52 |       24 |     54% |23-44, 61, 65, 67 |
| src/ub\_agents/monitor/eligible.py               |       44 |        0 |    100% |           |
| src/ub\_agents/monitor/local.py                  |       18 |        2 |     89% |     18-19 |
| src/ub\_agents/monitor/server.py                 |      113 |        9 |     92% |127, 129, 135-136, 168-172 |
| src/ub\_agents/monitor/snapshot.py               |      101 |        7 |     93% |69, 121-123, 146, 175-176 |
| src/ub\_agents/notices.py                        |      234 |        2 |     99% |  151, 238 |
| src/ub\_agents/observation\_data.py              |      105 |        1 |     99% |        29 |
| src/ub\_agents/observation\_worker.py            |      145 |       13 |     91% |24, 36-37, 49, 58-59, 62-63, 68, 72-73, 97, 116 |
| src/ub\_agents/observations.py                   |      568 |       10 |     98% |84, 102, 108-109, 133, 321, 379, 406, 540, 698 |
| src/ub\_agents/own\_merge.py                     |       24 |        1 |     96% |        15 |
| src/ub\_agents/pass\_messages.py                 |       54 |        0 |    100% |           |
| src/ub\_agents/poll\_now.py                      |       68 |        1 |     99% |        29 |
| src/ub\_agents/polling.py                        |       45 |        0 |    100% |           |
| src/ub\_agents/prompts.py                        |        4 |        0 |    100% |           |
| src/ub\_agents/queue\_order.py                   |       58 |        0 |    100% |           |
| src/ub\_agents/queue\_planner.py                 |      363 |        3 |     99% |195, 426, 429 |
| src/ub\_agents/rate\_limits.py                   |       29 |        1 |     97% |        44 |
| src/ub\_agents/read\_input.py                    |       16 |        1 |     94% |        23 |
| src/ub\_agents/records.py                        |      321 |       27 |     92% |67, 88, 123, 125, 139, 142, 154, 198, 218, 221, 324-326, 332, 334, 338, 345, 351, 353, 355, 357, 389, 391, 393, 395, 397, 399, 413 |
| src/ub\_agents/refresh.py                        |      151 |        9 |     94% |84, 90, 93-94, 110, 114-115, 133-134 |
| src/ub\_agents/renewal.py                        |       35 |        0 |    100% |           |
| src/ub\_agents/report\_command.py                |       12 |        5 |     58% |     17-23 |
| src/ub\_agents/retrospective.py                  |       10 |        0 |    100% |           |
| src/ub\_agents/run\_branches.py                  |        5 |        0 |    100% |           |
| src/ub\_agents/run\_config.py                    |       67 |        3 |     96% |82, 97, 101 |
| src/ub\_agents/run\_history.py                   |       37 |        0 |    100% |           |
| src/ub\_agents/run\_planning.py                  |      186 |        9 |     95% |94, 101-107, 209, 223 |
| src/ub\_agents/run\_prompt.py                    |       38 |        0 |    100% |           |
| src/ub\_agents/settlement.py                     |      131 |        5 |     96% |69, 96, 116, 121, 134 |
| src/ub\_agents/signals.py                        |       46 |        2 |     96% |     26-27 |
| src/ub\_agents/startup\_cleanup.py               |       64 |        1 |     98% |        18 |
| src/ub\_agents/state.py                          |       24 |        0 |    100% |           |
| src/ub\_agents/status.py                         |       51 |        0 |    100% |           |
| src/ub\_agents/trust.py                          |       49 |        1 |     98% |        27 |
| src/ub\_agents/uberblick.py                      |        7 |        0 |    100% |           |
| src/ub\_agents/updates/\_\_init\_\_.py           |        0 |        0 |    100% |           |
| src/ub\_agents/updates/runtime\_installations.py |      115 |        4 |     97% |51, 93, 134, 138 |
| src/ub\_agents/updates/runtime\_updates.py       |      300 |       21 |     93% |79, 106-107, 117, 137, 148-149, 156-157, 166-167, 183, 203, 211, 221-222, 234-235, 310-311, 339 |
| src/ub\_agents/updates/self\_updates.py          |      197 |       19 |     90% |40, 43, 58, 60, 77, 96, 98, 105, 123, 163-165, 176-177, 215, 217, 225, 232, 242 |
| src/ub\_agents/updates/updates.py                |      171 |       10 |     94% |73, 106-107, 126, 146, 163-164, 181, 208-209 |
| src/ub\_agents/usage/\_\_init\_\_.py             |        0 |        0 |    100% |           |
| src/ub\_agents/usage/context\_usage.py           |       75 |        0 |    100% |           |
| src/ub\_agents/usage/request\_stats.py           |       41 |        0 |    100% |           |
| src/ub\_agents/usage/run\_usage.py               |       34 |        0 |    100% |           |
| src/ub\_agents/usage/runtime\_usage.py           |       45 |        0 |    100% |           |
| src/ub\_agents/usage/usage\_output.py            |      117 |        2 |     98% |  110, 123 |
| src/ub\_agents/view/\_\_init\_\_.py              |        0 |        0 |    100% |           |
| src/ub\_agents/view/\_\_main\_\_.py              |        2 |        0 |    100% |           |
| src/ub\_agents/view/clipboard.py                 |       23 |        2 |     91% |     30-31 |
| src/ub\_agents/view/data.py                      |      389 |        8 |     98% |34, 75, 78, 122-123, 125-126, 133 |
| src/ub\_agents/view/github.py                    |      222 |       13 |     94% |78, 125, 152-155, 164-166, 170, 193-195 |
| src/ub\_agents/view/log\_pane.py                 |      169 |        3 |     98% |205, 214-215 |
| src/ub\_agents/view/logs.py                      |      103 |        2 |     98% |  108, 139 |
| src/ub\_agents/view/main.py                      |      136 |       12 |     91% |46-47, 71-72, 81-82, 117-118, 121, 129, 152, 164 |
| src/ub\_agents/view/markdown.py                  |       31 |        0 |    100% |           |
| src/ub\_agents/view/request.py                   |       26 |        3 |     88% |     19-21 |
| src/ub\_agents/view/runs.py                      |      153 |        0 |    100% |           |
| src/ub\_agents/view/scroll.py                    |       63 |        0 |    100% |           |
| src/ub\_agents/view/spinner.py                   |        4 |        0 |    100% |           |
| src/ub\_agents/view/status.py                    |      241 |        0 |    100% |           |
| src/ub\_agents/view/theme.py                     |       23 |        0 |    100% |           |
| src/ub\_agents/view/time.py                      |       28 |        0 |    100% |           |
| src/ub\_agents/view/ui.py                        |      934 |        9 |     99% |242, 334, 577, 694-695, 837, 917, 969, 1057 |
| src/ub\_agents/view/unblock.py                   |      155 |        2 |     99% |   80, 187 |
| src/ub\_agents/view/widgets.py                   |       89 |        0 |    100% |           |
| src/ub\_agents/view/work.py                      |      474 |        4 |     99% |37-38, 96, 318 |
| src/ub\_agents/view/worker.py                    |      118 |       17 |     86% |60-61, 97, 112, 117-118, 130-134, 140-146 |
| src/ub\_agents/worktree\_setup.py                |       64 |        1 |     98% |        47 |
| **TOTAL**                                        | **18346** |  **688** | **96%** |           |


## Setup coverage badge

Below are examples of the badges you can use in your main branch `README` file.

### Direct image

[![Coverage badge](https://raw.githubusercontent.com/uberblick-ai/ub-agents/python-coverage-comment-action-data/badge.svg)](https://htmlpreview.github.io/?https://github.com/uberblick-ai/ub-agents/blob/python-coverage-comment-action-data/htmlcov/index.html)

This is the one to use if your repository is private or if you don't want to customize anything.

### [Shields.io](https://shields.io) Json Endpoint

[![Coverage badge](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/uberblick-ai/ub-agents/python-coverage-comment-action-data/endpoint.json)](https://htmlpreview.github.io/?https://github.com/uberblick-ai/ub-agents/blob/python-coverage-comment-action-data/htmlcov/index.html)

Using this one will allow you to [customize](https://shields.io/endpoint) the look of your badge.
It won't work with private repositories. It won't be refreshed more than once per five minutes.

### [Shields.io](https://shields.io) Dynamic Badge

[![Coverage badge](https://img.shields.io/badge/dynamic/json?color=brightgreen&label=coverage&query=%24.message&url=https%3A%2F%2Fraw.githubusercontent.com%2Fuberblick-ai%2Fub-agents%2Fpython-coverage-comment-action-data%2Fendpoint.json)](https://htmlpreview.github.io/?https://github.com/uberblick-ai/ub-agents/blob/python-coverage-comment-action-data/htmlcov/index.html)

This one will always be the same color. It won't work for private repos. I'm not even sure why we included it.

## What is that?

This branch is part of the
[python-coverage-comment-action](https://github.com/marketplace/actions/python-coverage-comment)
GitHub Action. All the files in this branch are automatically generated and may be
overwritten at any moment.