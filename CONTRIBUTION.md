# Contributing

Contributions are welcome. The [PRD](docs/PRD/Supabase-AWS.md) defines the project's goals and boundaries; [GitHub issues](https://github.com/vincenzodomina/supabase-aws/issues) track findings and opportunities.

1. **Open an issue first.** Check existing issues, then use the [issue template](.github/ISSUE_TEMPLATE/change_request.md). Link the relevant PRD stories and related issues. Describe the problem, user story, expected behavior, and observable acceptance criteria.
2. **Write the issue and PR bodies yourself.** Treat them as concise, human-written change prompts: explain the intent and behavior in your own words. Include implementation details where they explain a material decision. Contributors remain responsible for understanding and validating the implementation.
3. **Submit a focused PR.** Use the [PR template](.github/pull_request_template.md), link the prior issue, and reference the same PRD stories. Explain any change in scope. Proposals outside the current PRD must identify the required requirement change in the issue and update the PRD in the PR.
4. **Keep issue tracking current.** State which acceptance criteria the PR satisfies and keep remaining work in a linked open issue. Only close an issue when its acceptance criteria are met; a PR that leaves work untracked must not be merged.

Changes should preserve the project's minimal structure and justify additional dependencies, AWS resources, or deployment machinery. Privacy, compliance, and enterprise integration take priority over savings. Implementations are reviewed against the linked acceptance criteria, with attention to RDS compatibility and independently managed ECS services.

Run the relevant [validation checks](README.md#validate) and report results, versions where relevant, and any checks not run. Keep tests proportionate to risk and reuse the existing suite. Real AWS provisioning and acceptance remain separately approved manual work; local checks do not establish RDS compatibility. Update affected documentation and explain migration or recovery implications.
