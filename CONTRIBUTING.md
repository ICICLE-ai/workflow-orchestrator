# Contributing

Thank you for helping improve this project. Contributions may include bug reports, documentation improvements, tests, examples, workflow or configuration artifacts, data/annotation schemas, and code changes.

## Before contributing

1. Read the [README](README.md) and relevant documentation.
2. Set up a local environment — the README's *Setup & Running Locally* section covers the Postgres container, backend, and frontend.
3. Review open issues and pull requests to avoid duplicate work.
4. Do not submit credentials, private keys, proprietary data, restricted data, sensitive locations, personally identifiable information, or material that you are not authorized to share.
5. Use the issue templates to report a problem or propose a change before beginning a substantial contribution.

## Adding a workflow step

Most contributions to this repository are new **steps** for the workflow canvas. Two guides cover the whole path, and between them document ports, run configuration, and the runtime execution model:

- [docs/adding-a-step-form.md](docs/adding-a-step-form.md) — define a step entirely in the backend with a `step.json`; the configuration form is generated for you. Also the reference for **ports**, **run configuration**, and **how a step is executed at run time**.
- [docs/adding-a-step-custom-ui.md](docs/adding-a-step-custom-ui.md) — replace the generated form with a custom React panel.

A new step should keep its `step_type_key` stable, declare its ports, and pass the startup registry sync without a `SKIPPING step` warning in the backend log.

## Contribution pathway

The project welcomes contributions in increasing order of technical and maintenance responsibility:

1. Execute an example and report a problem.
2. Improve documentation or examples.
3. Add or improve a test.
4. Propose a workflow, configuration, annotation, or other non-code artifact.
5. Prepare a bounded code contribution.

For step-authoring requirements specifically, follow the guides in [`docs/`](docs/) listed above.

## Pull requests

A pull request should:

- Reference the related issue or explain the problem being addressed.
- Be limited to one coherent change.
- Include or update tests when practical.
- Update documentation when user-visible behavior, interfaces, configuration, installation, or limitations change.
- Identify dependencies, data assumptions, security implications, and maintenance implications.
- Not include secrets, large unreviewed binary assets, private datasets, or unlicensed materials.

Maintainers may request changes, defer a contribution, or decline it when the change lacks a clear maintenance owner, conflicts with project scope, introduces unacceptable security or data risks, or cannot be reviewed with available resources.

## License and contributor rights

By submitting a contribution, you represent that you have the right to submit it and that it may be distributed under this repository's license. If your employer, institution, funder, or data provider imposes restrictions, obtain authorization before contributing.

## Security issues

Do not report suspected vulnerabilities in a public issue. Follow `SECURITY.md`.
