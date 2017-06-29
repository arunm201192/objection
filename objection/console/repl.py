import logging
import os
import sys

import click
import delegator
import frida
import pygments.styles
from prompt_toolkit import AbortAction, prompt
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.history import FileHistory
from prompt_toolkit.interface import AcceptAction
from prompt_toolkit.styles import default_style_extensions, style_from_dict
from pygments.style import Style
from pygments.token import Token

from .completer import CommandCompleter
from .repository import COMMANDS
from ..__init__ import __version__
from ..commands.device import get_device_info
from ..state.connection import state_connection
from ..utils.helpers import get_tokens


class PromptStyle(Style):
    def __init__(self):
        self.style = self._init_style()

    def _init_style(self):
        style = pygments.styles.get_style_by_name('vim')

        styles = {}
        styles.update(style.styles)
        styles.update(default_style_extensions)

        styles.update({
            # completions
            Token.Menu.Completions.Completion.Current: 'bg:#00aaaa #000000',
            Token.Menu.Completions.Completion: 'bg:#008888 #ffffff',
            Token.Menu.Completions.ProgressButton: 'bg:#003333',
            Token.Menu.Completions.ProgressBar: 'bg:#00aaaa',

            # User input.
            Token: '#ff0066',

            # Prompt.
            Token.Devicename: '#ff0000',
            Token.On: '#00aa00',
            Token.Devicetype: '#00ff48',
            Token.Version: '#00ff48',
            Token.Connection: '#717171'
        })

        return style_from_dict(styles)

    def get_style(self):
        return self.style


class Repl(object):
    """
        The exploration REPL for objection
    """

    def __init__(self):
        self.cli = None
        self.prompt_tokens = []

        self.completer = CommandCompleter()
        self.repository = COMMANDS

    def set_prompt_tokens(self, device_info):
        """
            Set prompt tokens sourced from a command.device.device_info()
            call.

            :param device_info:
            :return:
        """

        device_name, system_name, model, system_version = device_info

        self.prompt_tokens = [
            (Token.Devicename, device_name),
            (Token.On, ' on '),
            (Token.Devicetype, '(' + model + ': '),
            (Token.Version, system_version + ') '),
            (Token.Connection, '[' + state_connection.get_comms_type_string() + '] # '),
        ]

    def get_prompt_tokens(self, cli):
        """
            Return prompt tokens to use in the cli app.

            If none were set during the init of this class, it
            is assumed that the connection failed.

            :param cli:
            :return:
        """

        if self.prompt_tokens:
            return self.prompt_tokens

        return [
            (Token.Devicename, 'unknown device'),
            (Token.On, ''),
            (Token.Devicetype, ''),
            (Token.Version, ' '),
            (Token.Connection, '[' + state_connection.get_comms_type_string() + '] # '),
        ]

    def run_command(self, document):

        logging.info(document)

        if document.strip() == '':
            return

        # handle os commands
        if document.strip().startswith('!'):
            os_cmd = document[1:]

            click.secho('Running OS command: {0}\n'.format(os_cmd), dim=True)

            o = delegator.run(os_cmd)

            # print stdout
            if len(o.out) > 0:
                click.secho(o.out, bold=True)

            # print stderr
            if len(o.err) > 0:
                click.secho(o.err, fg='red')

            return

        # a normal command is to be run, extract the tokens and
        # find which method we should be calling
        tokens = get_tokens(document)

        # check if we should be presenting help instead of executing
        # a command
        if len(tokens) > 0 and 'help' == tokens[0]:
            tokens.remove('help')
            command_help = self._find_command_help(tokens)

            if not command_help:
                click.secho('No help found for: {0}'.format(' '.join(tokens)), fg='yellow')
                return

            # output the help and leave
            click.secho(command_help, fg='blue', bold=True)
            return

        # find an execution method to run
        token_matches, exec_method = self._find_command_exec_method(tokens)

        if exec_method is None:
            click.secho('Unknown or ambiguous command: `{0}`. Try `help {0}`.'.format(document), fg='yellow')
            return

        # strip the command matching tokens and leave
        # the rest as arguments
        arguments = tokens[token_matches:]

        # run the method for the command itself!
        exec_method(arguments)

    def _find_command_exec_method(self, tokens):
        """
            Attempt to find the actual python method to run
            for the command tokens we have.

            This is done by 'walking' the command dictionary,
            looking for the deepest 'exec' method definition. We are
            interested in the number of tokens walked as well, so
            that the calling command can know how many tokens to
            strip, sending the rest as arguments to the exec method.

            :param tokens:
            :return:
        """

        # start with all of the commands we have
        dict_to_walk = self.repository['commands']
        exec_method = None

        # keep count of the number of tokens
        # used in this walk
        walked_tokens = 0

        for token in tokens:

            # increment the walked tokens
            walked_tokens += 1

            # check if the token matches a command
            if token in dict_to_walk:

                # matched a dict for the token we have. we need
                # to have *all* of the tokens patch a nested dict
                # so that we can extract the final 'exec' key.
                # if we encounter a key that does not have nested commands,
                # chances are we are where we need to exec a command.
                if 'commands' not in dict_to_walk[token]:

                    if 'exec' in dict_to_walk[token]:
                        exec_method = dict_to_walk[token]['exec']
                        break

                else:
                    dict_to_walk = dict_to_walk[token]['commands']

            # stop if there is nothing that matches
            else:
                break

        return walked_tokens, exec_method

    def _find_command_help(self, tokens: list):
        """
            Attempt to find help for a command.

            Just like how the _find_command_exec_method works, this
            method also walks the command dictionary, searching for
            the deepest 'help' key.

            :param tokens:
            :return:
        """

        # start with all of the commands we have
        dict_to_walk = self.repository['commands']
        user_help = None

        for token in tokens:

            # check if the token matches a command
            if token in dict_to_walk:

                # matched a dict for the token we have. we need
                # to have *all* of the tokens patch a nested dict
                # so that we can extract the final 'help' key.
                # if we encounter a key that does not have nested commands,
                # chances are we are where we need to get help.
                if 'help' in dict_to_walk[token]:
                    user_help = dict_to_walk[token]['help']

                # if there are subcommands, continue with the walk
                if 'commands' not in dict_to_walk[token]:

                    if 'help' in dict_to_walk[token]:
                        user_help = dict_to_walk[token]['help']
                        break

                else:
                    dict_to_walk = dict_to_walk[token]['commands']

            # stop if we dont have a token that matches anything
            else:
                break

        return user_help

    def handle_exit(self, document):
        """
            Exit the repl if needed
        """

        if document.strip() in ('quit', 'exit', 'bye'):
            click.secho('Exiting...', dim=True)
            sys.exit()

    def handle_reconnect(self, document):

        if document.strip() in ('reconnect', 'reset'):
            click.secho('Reconnecting...', dim=True)

            try:
                self.set_prompt_tokens(get_device_info())
                click.secho('Reconnection succesful!', fg='green')

            except frida.ServerNotRunningError as e:
                click.secho('Failed to reconnect with error: {0}'.format(e.message), fg='red')

            return True

        return False

    def start_repl(self):
        """
            Start the repl built in self.cli
        """

        banner = ("""
     _     _         _   _
 ___| |_  |_|___ ___| |_|_|___ ___
| . | . | | | -_|  _|  _| | . |   |
|___|___|_| |___|___|_| |_|___|_|_|
        |___|(object)inject(ion) v{0}

     Runtime Mobile Exploration
        by: @leonjza from @sensepost
""").format(__version__)

        click.secho(banner, bold=True)
        click.secho('[tab] for command suggestions', fg='white', dim=True)

        while True:

            try:

                document = prompt(
                    get_prompt_tokens=self.get_prompt_tokens,
                    completer=self.completer,
                    style=PromptStyle().get_style(),
                    history=FileHistory(os.path.expanduser('~/.objection/objection_history')),
                    auto_suggest=AutoSuggestFromHistory(),
                    accept_action=AcceptAction.RETURN_DOCUMENT,
                    on_abort=AbortAction.RETRY,
                    reserve_space_for_menu=4
                )

                # check if this is an exit command
                self.handle_exit(document)

                # if we got the reconnect command, handle just that
                if self.handle_reconnect(document):
                    continue

                # find something to run
                self.run_command(document)

            except (KeyboardInterrupt, EOFError):

                # attempt to cleanly exit
                self.handle_exit('exit')