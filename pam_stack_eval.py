"""PAM stack parsing and strict control-flow evaluation (skeleton)."""


class PamError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class PamStackEvaluator:
    def __init__(self):
        pass

    def load_config(self, service, text):
        raise NotImplementedError

    def load_configs(self, service, configs):
        raise NotImplementedError

    def version(self, service):
        raise NotImplementedError

    def evaluate(self, service, user, *, password="", handlers=None):
        raise NotImplementedError

    def audit_events(self):
        raise NotImplementedError
