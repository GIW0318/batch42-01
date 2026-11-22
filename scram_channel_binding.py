"""SASL SCRAM nonce, channel binding and proof state machine (skeleton)."""


class ScramError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class ScramChannelBinding:
    def __init__(self, min_iterations=4096, max_failures=5):
        self.min_iterations = min_iterations
        self.max_failures = max_failures

    def register_user(self, username, *, salt, stored_key, server_key,
                      iterations=4096):
        raise NotImplementedError

    def change_password(self, username, *, salt, stored_key, server_key,
                        iterations=4096):
        raise NotImplementedError

    def start(self, client_first, *, connection_id="", tls_binding=b"",
              client_ip="0.0.0.0"):
        raise NotImplementedError

    def finish(self, client_final, *, client_ip="0.0.0.0"):
        raise NotImplementedError

    def auth_message(self, client_first_bare, server_first,
                     client_final_without_proof):
        raise NotImplementedError

    def audit_events(self):
        raise NotImplementedError
