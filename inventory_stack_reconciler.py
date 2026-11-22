class StackConflictError(Exception):
    def __init__(self, code="CONFLICT", message=""):
        super().__init__(message or code)
        self.code = code


class StackReconciler:
    def __init__(self, bag_size=200, warehouse_size=500, mail_size=50):
        self.bag_size = bag_size
        self.warehouse_size = warehouse_size
        self.mail_size = mail_size
        self._slots = {}
        self._journal = []
        self._applied = set()
        self._pending = []
        self._next_op = 1

    def put(self, container, index, item_id, count, bind=False, durability=100,
            max_stack=99):
        raise NotImplementedError()

    def slot(self, container, index):
        raise NotImplementedError()

    def slots(self, container):
        raise NotImplementedError()

    def find_free(self, container, item_id, need, bind=False, durability=100,
                  max_stack=99):
        raise NotImplementedError()

    def move(self, op_id, src, dst, count=None, expected_version=None,
             crash_midway=False):
        raise NotImplementedError()

    def split(self, op_id, src, count, dst):
        raise NotImplementedError()

    def merge(self, op_id, src, dst):
        raise NotImplementedError()

    def destroy(self, op_id, src, count):
        raise NotImplementedError()

    def organize(self, op_id, container):
        raise NotImplementedError()

    def total(self, item_id, include_mail=True):
        raise NotImplementedError()

    def sell(self, op_id, container, index, count, unit_price):
        raise NotImplementedError()

    def extract_mail(self, op_id, mail_index, dest_container):
        raise NotImplementedError()

    def resume_pending(self):
        raise NotImplementedError()

    def journal(self):
        raise NotImplementedError()

    def snapshot(self):
        raise NotImplementedError()

    def restore(self, snapshot):
        raise NotImplementedError()
