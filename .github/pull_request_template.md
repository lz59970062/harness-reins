## What changes and why

<!-- What a user or maintainer will see. Link the issue, or the Link PR number from docs/design/link-protocol.md section 12. -->

## How it was tested

<!-- The commands you ran and what happened. -->

**Real harness run** (required if this touches a delivery path):

- Harness and version:
- OS:
- What you did and what happened:

**Not tested:**

<!-- Be specific, for example "not run on Windows". Say "nothing" only if that is true. -->

## Checklist

- [ ] Tests fail without this change
- [ ] Failure cases are covered: dropped connection, restart, closed session, other version (as relevant)
- [ ] Works on Python 3.9
- [ ] No new `KNOWN_VIOLATIONS` entries
- [ ] No test was skipped or weakened, or the reason is explained above
- [ ] No new runtime dependency, or it was agreed in an issue
- [ ] `CHANGELOG.md` has an `Unreleased` entry, if users will notice this
- [ ] Public docs are updated in Chinese and English, if behaviour changed

## Security

Does this touch authentication, pairing, peer permissions, network listeners, remote start, or how peer data reaches a model?

- [ ] No
- [ ] Yes: I checked it against `docs/design/link-threat-model.md` and listed the affected threats and tests below
