"""Sudoers alias resolution and command matching compiler (skeleton)."""


class SudoersError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class SudoersCompiler:
    def __init__(self):
        pass

    def set_groups(self, mapping):
        raise NotImplementedError

    def compile(self, text):
        raise NotImplementedError

    def compile_files(self, files):
        raise NotImplementedError

    def check(self, user, host, command, args=()):
        raise NotImplementedError

    def effective_defaults(self, user):
        raise NotImplementedError

    def audit_events(self):
        raise NotImplementedError
