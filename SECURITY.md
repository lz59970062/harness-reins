# Security Policy

## Reporting a vulnerability

Please report vulnerabilities privately through
[GitHub Security Advisories](https://github.com/lz59970062/harness-reins/security/advisories/new).
Do not open a public issue, PR or discussion.

Include the affected version, how to reproduce it and what an attacker gains. A
minimal proof of concept helps.

Reins is maintained on a best-effort basis, so there is no guaranteed response
time. Please give us a reasonable chance to release a fix before you publish
details.

## Supported versions

Security fixes go to the latest release on npm.

## In scope

- Anything that lets someone outside the OS account read or change Reins state, or
  run commands as that account
- Bypassing peer authentication, permissions or revocation
- Getting a callback or message delivered to a session it was not addressed to
- Changing the structure of a callback envelope or its ACK command through task or
  peer data
- Secrets written to logs or returned in errors

## Out of scope

- Processes under the same OS account reading `~/.reins`. Reins does not treat that
  as a security boundary.
- A model choosing to follow instructions in a message from an authenticated peer,
  unless Reins presented that content as coming from the user or the system
- Anything that already requires control of the account

The design and its accepted risks are in
[the Link threat model](docs/design/link-threat-model.md).
