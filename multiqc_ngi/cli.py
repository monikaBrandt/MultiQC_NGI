#!/usr/bin/env python
""" MultiQC command line options - we tie into the MultiQC
core here and add some new command line parameters. """

import click

pid_option = click.option('--project',
    type = str,
    help = 'Manually specify a project in StatusDB instead of detecting automatically'
)
push_flag = click.option('--push/--no-push', 'push_statusdb',
    default = None,
    help = 'Push / do not push MultiQC results to StatusDB analysis db. Overrides config option push_statusdb'
)