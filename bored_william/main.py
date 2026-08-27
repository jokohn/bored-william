"""Single entry point for every stage of the pipeline.

Each stage owns its own argument parser -- they have little in common and
sharing one would force the union of every flag onto all three. This dispatches
on the first argument and hands the rest to the stage, so `bored-william read
--help` prints the reader's own help rather than a merged wall.

One command, one way in. The alternative -- a separate console script per stage
-- is what this replaced: three executables, three invocation styles, and no
single place that tells you the pipeline has three stages at all.
"""

import sys

# Imported lazily inside dispatch(): the reader stages need `anthropic` and
# `pillow`, which are an optional extra. Importing them at module scope would
# make `bored-william fetch` fail on a machine that only wants stage 1.
STAGES = {
    "fetch": (
        "bored_william.fetch",
        "Capture billboard images from Street View share links",
    ),
    "calibrate": (
        "bored_william.reader.calibrate",
        "Measure each board's real position from an existing capture set",
    ),
    "read": (
        "bored_william.reader.read",
        "Read captured images into a machine-readable advertising dataset",
    ),
}

USAGE = """\
usage: bored-william <stage> [options]

Turn Street View share links into a machine-readable dataset of roadside
billboard advertising, across the full capture history of each board.

stages:
  fetch      %s
  calibrate  %s
  read       %s

Run `bored-william <stage> --help` for a stage's own options.

A typical run, in order:
  bored-william fetch     --input boards.csv --outdir ./wide --fov 45
  bored-william calibrate --manifest ./wide/manifest.csv --boards boards.csv --outdir ./cal
  bored-william fetch     --input ./cal/billboards_calibrated.csv --outdir ./tight --fov 22
  bored-william read      --manifest ./tight/manifest.csv --outdir ./readings
""" % tuple(desc for _, desc in STAGES.values())


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE, end="")
        return 0
    if argv[0] in ("-V", "--version"):
        from . import __version__
        print("bored-william %s" % __version__)
        return 0

    stage = argv[0]
    if stage not in STAGES:
        print("bored-william: unknown stage %r\n" % stage, file=sys.stderr)
        print(USAGE, end="", file=sys.stderr)
        return 2

    module_path = STAGES[stage][0]
    try:
        module = __import__(module_path, fromlist=["main"])
    except ImportError as exc:
        # The reader stages depend on the optional extra; say so plainly
        # rather than surfacing a bare traceback about a missing package.
        print("bored-william %s: %s\n\nThis stage needs the reader extra:\n"
              "    pip install -e \".[reader]\"" % (stage, exc), file=sys.stderr)
        return 1
    return module.main(argv[1:])
