import collections
import logging
import numbers

import claripy

from pakala import env
from pakala import utils

from web3 import Web3


logger = logging.getLogger(__name__)


class BaseAnalyzer(object):
    """Base class for an Analyzer.

    Child classes need to define `caller` and `address`.
    """

    def __init__(self, max_wei_to_send, min_wei_to_receive, block='latest'):
        self.web3 = Web3()
        self.web3.eth.defaultBlock = block
        self.max_wei_to_send = max_wei_to_send
        self.min_wei_to_receive = min_wei_to_receive
        self.storage_cache = {}

    def _read_storage(self, state, key):
        logger.debug("Reading storage %r" % key)
        try:
            keys = state.solver.eval(key, 2)
            if len(keys) > 1:
                logger.info("Multiple keys possible for key %r", key)
                return utils.bvv(0)
            assert len(keys) == 1
            key = keys[0]
            assert isinstance(key, numbers.Number)
        except (claripy.errors.BackendError, claripy.errors.ClaripyFrontendError) as e:
            # Should not be too bad, because for the same key we will reuse the
            # same cache.
            logger.debug(
                "Encountered an exception when resolving key %r: %r", key, e)
            return utils.bvv(0)

        if key in self.storage_cache:
            value = self.storage_cache[key]
        else:
            hex_addr = self.web3.toChecksumAddress(
                    utils.number_to_address(
                        utils.bvv_to_number(self.address)))
            value = self.web3.toInt(self.web3.eth.getStorageAt(hex_addr, key))
            self.storage_cache[key] = value

        return utils.bvv(value)

    def check_state(self, state, path=None):
        """Check a reachable state for bugs"""
        logger.debug("Check state: %s", state)
        logger.debug("Constraints: %s", state.solver.constraints)

        read_constraints = []
        extra_constraints = []  # From the environment (block number, whatever)

        if path is None:
            path = [state]
            # Static read were we never wrote, but we know the key is not symbolic.
            # So we go and fetch it.
            for key, value in state.storage_read.items():
                constraint = state.storage_read[key] == self._read_storage(state, key)
                read_constraints.append(constraint)
                logger.debug("Add constraint: %s", constraint)

        for s in path:
            extra_constraints += s.env.extra_constraints()

        # Suicide
        if state.suicide_to is not None:
            final_balance = state.env.balance - sum(call[-3] for call in state.calls)
            constraints = extra_constraints + read_constraints + [
                final_balance > 0,
                state.suicide_to[159:0] == self.caller[159:0]]
            logger.debug("Check for suicide bug with constraints %s", constraints)
            if state.solver.satisfiable(extra_constraints=constraints):
                logger.info("Found suicide bug.")
                return True

        # Calls
        total_sent = sum(s.env.value for s in path)
        sent_constraints = [s.env.value < self.max_wei_to_send for s in path]
        total_received = None
        received_to = []

        for call in state.calls:
            value, to, gas = call[-3:] # pylint: disable=unused-variable,invalid-name
            if state.solver.satisfiable(
                    extra_constraints=[to[159:0] == self.caller[159:0]]):
                received_to.append(to)
                if total_received is None:
                    total_received = value
                else:
                    total_received += value

        if total_received is None:
            return False

        logger.debug("Found %i calls back to caller.", len(received_to))

        constraints = sent_constraints + extra_constraints + read_constraints + [
            state.env.balance >= total_received, # Enough money?
            total_received > total_sent, # I get more than what I sent?
            total_received > self.min_wei_to_receive,
            state.env.caller == utils.DEFAULT_CALLER,
            state.env.origin == utils.DEFAULT_CALLER]

        logger.debug("Extra constraints: %r", constraints)

        if state.solver.satisfiable(extra_constraints=constraints):
            logger.info("Found call bug.")
            return True

        return False


class Analyzer(BaseAnalyzer):
    """Simple Analyzer class, where caller and address are given explicitly."""

    def __init__(self, address, caller, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.caller = caller
        self.address = address


class FakeStorage(dict):
    """For testing, you can override the storage_cache with an instance of this
    class. This allow you to simulate storage for a contract, and prevent the
    code to try to fetch it from web3. Instead it will crash if it tries to
    access something that you didn't specify."""

    def __contains__(self, key):
        if not super().__contains__(key):
            raise KeyError("The analyzer is trying to access a FakeStorage"
                           " key that we didn't specify: '%s'." % key)
        return True


