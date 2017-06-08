import socket
import os
from time import time
import collections
from threading import Thread

DEFAULT_HOST = '0.0.0.0'
DEFAULT_PORT = 1666

DEFAULT_BIG_TOLERANCE = 2
DEFAULT_TOLERANCE = .05 # margin that is considered acceptable for slave to be ahead or behind
DEFAULT_GRACE_TIME = 3 # amount of time to wait with re-syncs after a resync
DEFAULT_JUMP_AHEAD = 3 # amount of time to jump ahead of master's playback position (giving slave enough time to load new keyframes)

class Receiver:
    def __init__(self, omxplayer, options = {}):
        # config
        self.player = omxplayer
        self.options = options
        self.verbose = options['verbose'] if 'verbose' in options else False
        self.big_tolerance = options['big_tolerance'] if 'big_tolerance' in options else DEFAULT_BIG_TOLERANCE
        self.tolerance = options['tolerance'] if 'tolerance' in options else DEFAULT_TOLERANCE
        self.grace_time = options['grace_time'] if 'grace_time' in options else DEFAULT_GRACE_TIME
        self.jump_ahead = options['jump_ahead'] if 'jump_ahead' in options else DEFAULT_JUMP_AHEAD

        # attributes
        self.socket = None
        self.received_position = None
        self.received_duration = None
        self.received_status = None
        self.last_measure_time = 0
        self.paused_until = None
        self.dont_sync_until = 0
        self.deviation = 0
        self.deviations = collections.deque(maxlen=10)
        self.median_deviation = 0
        self.duration_match = None
        self.rate = 1
        self.update_thread = Thread(target=self.update_loop())
        self.update_thread.start()

    def __del__(self):
        self.destroy()

    def setup(self):
        host = self.options['host'] if 'host' in self.options else DEFAULT_HOST
        port = self.options['port'] if 'port' in self.options else DEFAULT_PORT

        # create socket connections
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, 0)
        # non-blocking, please
        self.socket.setblocking(0)
        # bind to configured host/port
        self.socket.bind((host, port))

    def destroy(self):
        if self.socket:
            self.socket.close()
            self.socket = None

    def update_loop(self):
        while self.player.playback_status() != "Stopped":
            self.update()

    def update(self):
        # keep receiving data so don't get whole batch of data later
        data = self._receive_data()
        local_pos = self.player.position()
        if local_pos is None: # we'll need our own local position
            return
        local_status = self.player.playback_status()
        if local_status is None:
            return

        self.last_measure_time = time()

        # paused for master to catch-up?
        if self.paused_until:
            if self.last_measure_time < self.paused_until:
                # still waiting
                return
            # stop waiting and resume playback
            self.paused_until = None
            self.player.play()
            if self.verbose:
                print("resuming playback")
            self.dont_sync_until = self.last_measure_time + self.grace_time

        # no data? no action.
        if not data:
            return

        # store received data
        self.received_position = float(data[0])
        self.received_duration = float(data[1])
        self.received_status = data[2]

        if local_status != self.received_status:
            self.player.play_pause()

        if self.received_status == 'Paused':
            return

        # calculate current deviation based on newly received master position
        self.deviation = self.received_position - local_pos

        if self.verbose:
            print('PositionReceiver got: %s @ %.2f (deviation: %.2f, status: %s, rate: %s)' %
                  (self.received_duration, self.received_position, self.deviation, local_status, self.rate))

        # check file; if master is playing a different file, then there is no use in time-syncing
        if self.duration_match is None:
            if not self.received_duration == float(self.player.duration()):
                print('durations of files does not match! Master:%s Slave%s' %
                      (self.received_duration, self.player.duration()))
                return
            else:
                self.duration_match = True

        # calculate median deviation
        self.deviations.append(self.deviation)
        self.median_deviation = self._calculate_median(list(self.deviations))

        if self.verbose:
            print('PositionReceiver.median_deviation: ' + str(self.median_deviation))

        # still at start or end of video, don't sync
        if self.received_position <= self.grace_time:  # or self.player.position() <= self.grace_time:
            return

        if (self.received_duration - local_pos) < self.grace_time:
            if self.rate != 1:
                self._reset_small_sync()
            return

        # not deviated very much, nothing to sync
        if abs(self.median_deviation) <= self.tolerance:
            if self.rate != 1:
                self._reset_small_sync()
            return

        # still in post-sync gracetime
        if self.last_measure_time < self.dont_sync_until:
            return

        # ok, let's do some syncing
        self.deviations.clear()
        self._perform_small_sync()

    def _receive_data(self):
        try:
            # read incoming socket data
            pos, duration, playback_status = self.socket.recv(1024).decode('utf-8').split('%', 2)
            return (pos, duration, playback_status)
        except Exception as e:
            pass

        return None

    def _calculate_median(self, lst):
        quotient, remainder = divmod(len(lst), 2)
        if remainder:
            return sorted(lst)[quotient]
        return float(sum(sorted(lst)[quotient - 1:quotient + 1]) / 2.0)

    def _perform_small_sync(self):
        if self.deviation < 0 and self.rate > 0:
            self.player.action(1)
            self.rate -= 1
        elif self.deviation > 0 and self.rate < 2:
            self.player.action(2)
            self.rate += 1

    def _reset_small_sync(self):
            if self.rate == 2:
                self.player.action(1)
            elif self.rate == 0:
                self.player.action(2)
            self.rate = 1

    def _perform_big_sync(self):
        # negative deviation means we are ahead of master; if we're ahead but no by too much
        # (less that self.jump_ahead); don't jump, but simply pause until master catches up
        if self.deviation < 0 and abs(self.deviation) < self.jump_ahead:
            self.player.pause()
            # let's pause the amount of deviation time
            self.paused_until = self.last_measure_time - self.deviation
            if self.verbose:
                print("paused for %.2f seconds" % (-self.deviation))
            return

        # ok, so we're lacking behind
        # calculate position to jump to (bit ahead of master's playback position)
        pos = self.received_position + self.jump_ahead
        # pause and jump to calculated position
        self.player.pause()
        self.player.set_position(pos)
        # pause until the master should have caught up
        self.paused_until = self.last_measure_time + self.jump_ahead

        if self.verbose:
            print("jumped to position %.2f and paused for %.2f seconds" % (pos, self.jump_ahead))
