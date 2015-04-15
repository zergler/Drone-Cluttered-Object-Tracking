#!/usr/bin/env python2

import cv2
import json
import Queue
import numpy as np


# Local modules.
import remote
import camera
import controller
import receiver

# Feature modules.
from feature_extraction import hough_transform
from feature_extraction import optical_flow
from feature_extraction import laws_mask
from feature_extraction import history

# Tracking modules.
from tracking import bounding_box
from tracking import cam_shift


class ParrotError(Exception):
    """ Base exception for the module.
    """
    def __init__(self, msg):
        self.msg = 'Error: %s' % msg

    def print_error(self):
        print(self.msg)


class Parrot(object):
    """ Encapsulates the AR Parrot Drone 2.0.

        Allows access to the drone's front and bottom cameras, the ability to
        send commands, and the ability to read the dron's navigation data.
    """
    def __init__(self):
        self.default_cmd = {
            'X': 0.0,
            'Y': 0.0,
            'Z': 0.0,
            'R': 0.0,
            'C': 0,
            'T': False,
            'L': False,
            'S': False
        }
        self.address = '192.168.1.1'
        self.ports = {
            'NAVDATA': 5554,
            'VIDEO':   5555,
            'CMD':     5556
        }
        self.cameras = {
            'FRONT':  0,
            'BOTTOM': 3,
            'CUSTOM': 4
        }
        self.active_camera = self.cameras['FRONT']

        # Feature extraction parameters.
        self.window_size = (15, 7)
        self.overlap = 0.5

        # Where we get our features.
        self.image = None
        self.navdata = None

        # The method of tracking we are going to use.
        self.tracking = None

    def init_remote(self):
        """ Initializes the remote control.
        """
        self.remote_queue = Queue.Queue(maxsize=1)
        self.remote_bucket = Queue.Queue()
        self.remote = remote.Remote(self.remote_queue)
        self.remote.daemon = True
        self.remote.start()

        # Grab the initial remote data so we know it is initialized.
        self.cmd = self.remote_queue.get()

    def init_camera(self):
        """ Initializes the camera thread.
        """
        camera_address = 'tcp://' + self.address + ':' + str(self.ports['VIDEO'])
        self.image_queue = Queue.Queue(maxsize=1)
        self.camera = camera.Camera(camera_address, self.image_queue)
        self.camera.daemon = True
        self.camera.start()

        # Grab the initial image so we know the camera is initialized.
        self.image = self.image_queue.get()

    def init_controller(self):
        """ Initializes the controller thread.
        """
        self.cmd_queue = Queue.Queue(maxsize=1)
        self.controller = controller.Controller(self.cmd_queue)
        self.controller.daemon = True
        self.controller.start()

    def init_receiver(self):
        """ Initializes the receiver thread.
        """
        self.nav_queue = Queue.Queue(maxsize=1)
        self.receiver = receiver.Receiver(self.nav_queue)
        self.receiver.daemon = True
        self.receiver.start()

        # Grab the initial nav data so we know the receiver is initialized.
        self.navdata = self.nav_queue.get()

    def init_tracking(self, bound_box):
        """ Initializes tracking. Make sure the camera is initialized before
            calling this function.
        """
        assert self.image is not None

        if bound_box is None:
            raise bounding_box.BoundingBoxError()
        elif len(bound_box) != 2:
            raise bounding_box.BoundingBoxError()
        elif (len(bound_box[0]) != 2) or (len(bound_box[1]) != 2):
            raise bounding_box.BoundingBoxError()
        self.tracking = cam_shift.CamShift(self.image, bound_box[0], bound_box[1])

    def init_feature_extract(self):
        """ Initializes feature extraction. Make sure the camera and receiver
            are initialized before calling this function.
        """
        assert self.image is not None
        assert self.navdata is not None

        # Grab an example window from the initial image to feed the optical flow
        # feature extractor (use a non border window).
        windows = camera.Camera.get_windows(self.image, self.window_size, self.overlap)
        small_image = self.image[windows[1][1][2]:windows[1][1][3], windows[1][1][0]:windows[1][1][1]]

        # Initialize each feature extractor.
        self.extractor_opt_flow = optical_flow.OpticalFlow(small_image)
        self.extractor_hough_trans = hough_transform.HoughTransform()
        self.extractor_laws_mask = laws_mask.LawsMask()
        self.extractor_cmd_history = history.CmdHistory()
        self.extractor_nav_history = history.NavHistory()

    def check_remote(self):
        """ Checks the remote thread to see if it's okay.
        """
        # First make sure it is still running.
        okay = self.remote.isAlive()

        # Grab any exceptions it may have generated.
        try:
            error = self.remote_bucket.get(block=False)
            raise error
        except Queue.empty:
            pass
        except remote.RemoteError as e:
            # If the error is a warning, print the warning, otherwise exit.
            if e.warning:
                print(e.msg)
            else:
                raise

        return okay

    def get_visual_features(self):
        """ Gets the features of the images from the camera. Make sure the
            camera and feature extraction are initialized before calling this
            function. Allow the calling module to set the rate at which images
            are received from the camera thread.
        """
        assert self.image is not None
        assert self.extractor_opt_flow is not None
        assert self.extractor_hough_trans is not None
        assert self.extractor_laws_mask is not None

        # Get the windows from the current image.
        windows = camera.Camera.get_windows(self.image, self.window_size, self.overlap)

        # Arrays that will contain the different features.
        feats_all = np.array([])
        feats_flow = np.arrary([])
        feats_hough = np.arry([])
        feats_laws = np.array([])

        # Iterate through the windows, computing features for each.
        for r in range(0, self.window_size[1]):
            for c in range(0, self.window_size[0]):
                # Get the current window of the image for which the features
                # will be extracted from.
                cur_window = self.image[windows[r][c][2]:windows[r][c][3], windows[r][c][0]:windows[r][c][1]]

                # If the current window is a border window, it may have a
                # smaller size, so reshape it.
                cur_window = cv2.resize(cur_window, self.extractor_opt_flow.shape[::-1])

                # Get the optical flow features from the current window.
                flow = self.extractor_opt_flow.extract(cur_window)
                feats_cur = optical_flow.OpticalFlow.get_features(flow)
                feats_flow = np.vstack((feats_flow, feats_cur)) if feats_flow.size else feats_cur

                # Get the Hough transform features from the current window.
                feats_cur = self.extractor_hough_trans.extract(cur_window)
                feats_hough = np.vstack((feats_hough, feats_cur)) if feats_hough.size else feats_cur

                # Get the Law's texture mask features from the current window.
                feats_cur = self.extractor_laws_mask.extract(cur_window)
                feats_laws = np.vstack((feats_laws, feats_cur)) if feats_laws.size else feats_cur

        # Vertically stack all of the different features.
        feats_all = np.vstack((feats_all, feats_flow)) if feats_all.size else feats_flow
        feats_all = np.vstack((feats_all, feats_hough)) if feats_all.size else feats_hough
        feats_all = np.vstack((feats_all, feats_laws)) if feats_all.size else feats_laws

        # Transpose and return.
        return np.transpose(feats_all)

    def get_nav_features(self):
        """ Gets the features from the navigation data of the drone. Make sure
            the receiver is initialized before calling this function. Allow the
            calling module to set the rate at which navigation data is received
            from the receiver thread.
        """
        assert self.navdata is not None
        assert self.extractor_cmd_history is not None
        assert self.extractor_nav_history is not None
        return None

    def get_navdata(self):
        """ Receives the most recent navigation data from the drone. Calling
            module should call this function in a loop to get navigation data
            continuously. Make sure the receiver is initialized.
        """
        self.navdata = self.nav_queue.get()
        return self.navdata

    def get_image(self):
        """ Receives the most recent image from the drone. Calling module should
            call this function in a loop to get images continuously. Make sure
            the camera is initialized.
        """
        self.image = self.image_queue.get()
        return self.image

    def get_cmd(self):
        """ Receives the most recent command from the remote. Calling module
            should call this function in a loop to get the the commands
            continuously. Make sure the remote is initialized.
        """
        self.cmd = self.remote_queue.get()
        return self.cmd

    def send_cmd(self, cmd):
        """ Before exiting, safely lands the drone and closes all processes.
            Make sure the controller is initialized.
        """
        cmd_json = json.dumps(cmd)
        self.cmd_queue.put(cmd_json)

    def land(self):
        cmd = self.default_cmd.copy()
        cmd['L'] = True
        cmd_json = json.dumps(cmd)
        self.cmd_queue.put(cmd_json)

    def takeoff(self):
        cmd = self.default_cmd.copy()
        cmd['T'] = True
        cmd_json = json.dumps(cmd)
        self.cmd_queue.put(cmd_json)

    def stop(self):
        cmd = self.default_cmd.copy()
        cmd = self.default_cmd.copy()
        cmd['S'] = True
        cmd_json = json.dumps(cmd)
        self.cmd_queue.put(cmd_json)

    def turn_left(self, speed):
        cmd = self.default_cmd.copy()
        cmd['R'] = -speed
        cmd_json = json.dumps(cmd)
        self.cmd_queue.put(cmd_json)

    def turn_right(self, speed):
        cmd = self.default_cmd.copy()
        cmd['R'] = speed
        cmd_json = json.dumps(cmd)
        self.cmd_queue.put(cmd_json)

    def fly_up(self, speed):
        cmd = self.default_cmd.copy()
        cmd['Z'] = speed
        cmd_json = json.dumps(cmd)
        self.cmd_queue.put(cmd_json)

    def fly_down(self, speed):
        cmd = self.default_cmd.copy()
        cmd['Z'] = -speed
        cmd_json = json.dumps(cmd)
        self.cmd_queue.put(cmd_json)

    def fly_forward(self, speed):
        cmd = self.default_cmd.copy()
        cmd['Y'] = speed
        cmd_json = json.dumps(cmd)
        self.cmd_queue.put(cmd_json)

    def fly_backward(self, speed):
        cmd = self.default_cmd.copy()
        cmd['Y'] = -speed
        cmd_json = json.dumps(cmd)
        self.cmd_queue.put(cmd_json)

    def fly_left(self, speed):
        cmd = self.default_cmd.copy()
        cmd['X'] = -speed
        cmd_json = json.dumps(cmd)
        self.cmd_queue.put(cmd_json)

    def fly_right(self, speed):
        cmd = self.default_cmd.copy()
        cmd['X'] = speed
        cmd_json = json.dumps(cmd)
        self.cmd_queue.put(cmd_json)

    def change_camera(self, camera):
        cmd = self.fly.default_cmd.copy()
        cmd['C'] = camera
        cmd_json = json.dumps(cmd)
        self.cmd_queue.put(cmd_json)


def _test_parrot():
    """ Tests the parrot module
    """
    pdb.set_trace()
    parrot = Parrot()
    parrot.init_camera()
    parrot.init_receiver()
    parrot.init_feature_extract()

    while True:
        image = parrot.get_image()
        parrot.get_navdata()

        visual_features = parrot.get_visual_features()
        # nav_features = parrot.get_nav_features()
        with open('feat.dat', 'a') as out:
            np.savetxt(out, visual_features)

        cv2.imshow('Image', image)
        key = cv2.waitKey(1) & 0xFF
        if key == ord('q'):
            break

if __name__ == '__main__':
    import pdb
    _test_parrot()
