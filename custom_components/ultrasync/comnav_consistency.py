"""Keep ComNav bank updates and shared HTTP state consistent."""
import copy
import functools
import re
import threading
import types

from ultrasync.main import HubResponseType, NX595EVendor


def update_area_bank(self, bank=0):
    if not self.session_id and not self.login():
        return None
    response = self._UltraSync__get(
        '/user/status.xml', rtype=HubResponseType.XML,
        payload={'sess': self.session_id, 'arsel': bank},
    )
    if response is None:
        return None
    try:
        values = [int(response.find('stat{}'.format(i)).text) for i in range(17)]
    except (AttributeError, ValueError, TypeError):
        return None
    fault = response.find('sysflt')
    self._UltraSync__extra_area_status = re.split(r'\r*\n', fault.text) if fault is not None and fault.text else []
    # One XML bank contains bitmasks for eight areas, not just one area.
    for number, area in self.areas.items():
        if number // 8 == bank:
            area['bank_state'] = values.copy()
    return response


def install_consistency_fixes(hub):
    if getattr(hub, '_ha_consistency_installed', False):
        return
    # Vendor is only discovered at login; dispatch still chooses this method
    # solely for ComNav after login.
    hub._comnav_area_status_update = types.MethodType(update_area_bank, hub)
    lock = threading.RLock()

    def synchronized(method, snapshot=False):
        @functools.wraps(method)
        def call(*args, **kwargs):
            with lock:
                result = method(*args, **kwargs)
                return copy.deepcopy(result) if snapshot else result
        return call

    # Keep the lock inside executor threads even if an async waiter times out.
    # Reentrant because command verification invokes update() internally.
    for name in ('update', 'set_alarm', 'set_zone_bypass', 'set_output_control', 'details'):
        setattr(hub, name, synchronized(getattr(hub, name), snapshot=name == 'details'))
    hub._ha_consistency_installed = True
