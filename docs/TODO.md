# TODO — open decisions and stopped work

Entries here are owned by the maintainer. Each one carries a recommended
decision so nobody has to start from zero.

## Open decisions

### Product name (blocks any public launch)

"Lore" collides in three places (checked 2026-10-14): an unrelated framework
owns `lore` on PyPI, `getspecstory/lore` is a project of the same name for the
same audience, and another product named "Lore" was announced on Hacker News in
June 2026.

**Recommended decision:** rename before any public launch. Keep the import
package `lore` and the repository as they are until a name has been checked on
PyPI, GitHub and a trademark search; branding is not part of the engineering
program in `docs/superpowers/plans/2026-10-14-coverage-summaries-ui.md`.

### Distribution

Comparable tools ship a single binary or a desktop app. Lore is a Python package
with a Flask UI.

**Recommended decision:** keep Python. After tool coverage is finished, add a
`pipx`-installable entry point and a Docker image. Do not rewrite.

### macOS and Windows

Storage paths for those systems come from other projects' documentation and
were not verified on real machines.

**Recommended decision:** release Linux first and label the other systems
"best effort" until someone runs them.

### Competitors worth tracking

`agentsview`, `coding_agent_session_search` (cass) and `deja-vu` cover 33 to 65
tools. Licence notes: `basic-memory` is AGPL-3.0 and cass carries an additional
clause, so take ideas, not code. See `docs/research/2026-10-14-oss-landscape.md`.

## Stopped work

None yet. The overnight loop appends entries here when a task fails four times
in a row or needs something outside its constraints.
