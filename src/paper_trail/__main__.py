"""`python -m paper_trail` runs the same CLI as the `paper-trail` console script."""

from paper_trail.interfaces.cli.main import app

app(prog_name="paper-trail")
