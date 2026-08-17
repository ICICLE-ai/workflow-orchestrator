# Release Checklist

Complete this checklist before creating a public release.

## Product and documentation

- [ ] Version and release date are set.
- [ ] Release notes state current capabilities, known limitations, and breaking changes.
- [ ] README installation and reference example were reviewed against the release.
- [ ] `CITATION.cff` matches the tagged version and release date.
- [ ] Documentation identifies supported environments and known unsupported settings.

## Quality and security

- [ ] Relevant tests were run and results are recorded in the release notes or release record.
- [ ] Known test gaps are documented.
- [ ] Dependencies and configuration changes were reviewed.
- [ ] No secrets, credentials, private certificates, proprietary data, restricted data, or unauthorized artifacts are included.
- [ ] Security contact and vulnerability-reporting information are current.

## Governance and provenance

- [ ] The release was approved by an authorized maintainer.
- [ ] The release tag identifies the source commit.
- [ ] Public examples use public, permitted, synthetic, or de-identified data.
- [ ] Release artifacts can be traced to their source, configuration, dependencies, and test record.
