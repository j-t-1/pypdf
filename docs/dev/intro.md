# Developer Intro

pypdf is a library and hence its users are developers. This document is not for
the users, but for people who want to work on pypdf itself.

## Installing Requirements

```
pip install -r requirements/dev.txt
```

## Running Tests

See [testing pypdf with pytest](testing.md).

## The sample-files git submodule
The reason for having the submodule `sample-files` is that we want to keep
the size of the pypdf repository small while we also want to have an extensive
test suite. Those two goals contradict each other.

The `resources` folder should contain a select set of core examples that cover
most cases we typically want to test for. The `sample-files` might cover a lot
more edge cases, the behavior we get when file sizes get bigger, different
PDF producers.

To get the sample-files folder, you need to execute:

```
git submodule update --init
```

## Tools: git and pre-commit

Git is a command line application for version control. If you don't know it,
you can [play ohmygit](https://ohmygit.org/) to learn it.

GitHub is the service where the pypdf project is hosted. While git is free and
open source, GitHub is a paid service by Microsoft, but free in a lot of
cases.

[pre-commit](https://pypi.org/project/pre-commit/) is a command line application
that uses git hooks to automatically execute code. This allows you to avoid
style issues and other code quality issues. After you entered `pre-commit install`
once in your local copy of pypdf, it will automatically be executed when
you `git commit`.

## Commit Messages

Having a clean commit message helps people to quickly understand what the commit
is about, without actually looking at the changes. The first line of the
commit message is used to [auto-generate the CHANGELOG](https://github.com/py-pdf/pypdf/blob/main/make_release.py).
For this reason, the format should be:

```
PREFIX: DESCRIPTION

BODY
```

The `PREFIX` can be:

* `SEC`: Security improvements. Typically, an infinite loop that was possible.
* `BUG`: A bug was fixed. Likely there are one or multiple issues. Then write in
   the `BODY`: `Closes #123` where 123 is the issue number on GitHub.
   It would be absolutely amazing if you could write a regression test in those
   cases. That is a test that would fail without the fix.
   A bug is always an issue for pypdf users - test code or CI that was fixed is
   not considered a bug here.
* `ENH`: A new feature! Describe in the body what it can be used for.
* `DEP`: Deprecation. Either marking something as "this is going to be removed"
   or actually removing it.
* `PI`: A performance improvement. This could also be a reduction in the
        file size of PDF files generated by pypdf.
* `ROB`: A robustness change. Dealing better with broken PDF files.
* `DOC`: A documentation change.
* `TST`: Adding or adjusting tests.
* `DEV`: Developer experience improvements, e.g., pre-commit or setting up CI.
* `MAINT`: Quite a lot of different stuff. Performance improvements are, for sure,
           the most interesting changes in here. Refactorings as well.
* `STY`: A style change. Something that makes pypdf code more consistent.
         Typically, a small change. It could also be better error messages for
         end users.

The prefix is used to generate the CHANGELOG. Every PR must have exactly one -
if you feel like several match, take the top one from this list that matches for
your PR.

## Pull Request Size

Smaller Pull Requests (PRs) are preferred as it's typically easier to merge
them. For example, if you have some typos, a few code-style changes, a new
feature, and a bug-fix, that could be three or four PRs.

A PR must be complete. That means if you introduce a new feature, it must be
finished within the PR and have a test for that feature.

## Benchmarks

We need to keep an eye on performance, and thus we have a few benchmarks.

See [py-pdf.github.io/pypdf/dev/bench](https://py-pdf.github.io/pypdf/dev/bench/)
