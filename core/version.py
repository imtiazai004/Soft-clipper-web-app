"""One place that says which version this is.

The number used to live in installer.iss alone, which was fine while the
installer was the only thing that mentioned it. Now the update check compares it
against the latest GitHub release and the UI shows it in Settings, so a second
copy would eventually disagree with the first.

Bump this, then tag the release to match: `git tag v1.3.0`. The release workflow
passes the tag to Inno Setup, so the installer follows this file automatically.
"""

__version__ = "1.3.0"
