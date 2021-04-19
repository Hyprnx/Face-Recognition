import time
import warnings
from queue import Queue
from typing import List, Optional, Tuple

import cv2
import torch
import codecs
import pickle
import json
import onnx
import numpy as np
import requests
import unidecode
import onnx_tensorrt.backend as backend
from torchvision import transforms
from sklearn.metrics.pairwise import cosine_similarity

from face_recognition.dao import StudentDAO
from face_recognition.utils import draw_box
from face_recognition.utils import find_max_bbox, Cfg, download_weights
from face_recognition.utils import track_queue, check_change
from face_recognition.detection import FaceDetector
from face_recognition.align import warp_and_crop_face, get_reference_facial_points
from face_recognition.anti_spoofing import detect_spoof, AntiSpoofPredict


# Turn off warnming
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)
from memory_profiler import profile

def gstreamer_pipeline(
    capture_width=640,
    capture_height=480,
    display_width=640,
    display_height=480,
    framerate=60,
    flip_method=0,
):
    return (
        "nvarguscamerasrc ! "
        "video/x-raw(memory:NVMM), "
        "width=(int)%d, height=(int)%d, "
        "format=(string)NV12, framerate=(fraction)%d/1 ! "
        "nvvidconv flip-method=%d ! "
        "video/x-raw, width=(int)%d, height=(int)%d, format=(string)BGRx ! "
        "videoconvert ! "
        "video/x-raw, format=(string)BGR ! appsink"
        % (
            capture_width,
            capture_height,
            framerate,
            flip_method,
            display_width,
            display_height,
        )
    )

def main(tensorrt: bool, cam_device: Optional[int], input_size: Tuple[int, int], area_threshold=10000, score_threshold=0.6, cosin_threshold=0.4, padding_threshold=20):

	# base configure
	cpu = not torch.cuda.is_available()
	device = torch.device('cpu' if cpu else 'cuda:0')
	config: dict = Cfg.load_config()

	# Load Face Detection Model
	print("Load Face Detection Model")
	detection_model_path = download_weights(config['weights']['face_detections']['FaceDetector_pytorch'])
	face_detector = FaceDetector(detection_model_path, cpu=False, tensorrt=True, input_size=(480, 640))

	# Load reference of alignment
	reference = get_reference_facial_points(default_square=True)

	# Load Face Anti Spoof Models
	print("Load Face Anti Spoof Model")
	anti_spoof_names: List[str] = config['anti_spoof_name']
	model_spoofing = {}
	for model_name in anti_spoof_names:
		path = download_weights(config['weights']['anti_spoof_models'][model_name])
		model_spoofing[model_name] = AntiSpoofPredict(model_path=path)

	# Queue for tracking face
	faces_queue = Queue(maxsize=7)
	current_state: Optional[tuple] = None

	# Camera Configure
	camera = cv2.VideoCapture(gstreamer_pipeline(flip_method=4), cv2.CAP_GSTREAMER)
	count = 0

	while True:
		ret, frame = camera.read()

		im_height, im_width, _ = frame.shape
		if im_height != input_size[0] or im_width != input_size[1]:
		    raise Exception('Frame size must be {}'.format(input_size))

		if not ret:
		    break

		if count % 8 == 0:
			start = time.time()
			original_img = np.copy(frame)
			start_d = time.time()
			bounding_boxes = face_detector.detect(frame)
			print("face detection time", time.time() - start_d)
			remove_rows = list(np.where(bounding_boxes[:, 4] < score_threshold)[0]) # score_threshold
			bounding_boxes = np.delete(bounding_boxes, remove_rows, axis=0)

			if bounding_boxes.shape[0] != 0 and find_max_bbox(bounding_boxes, area_threshold=area_threshold) is not None:
				max_bbox = find_max_bbox(bounding_boxes)

				coordinate = [max_bbox[0], max_bbox[1], max_bbox[2], max_bbox[3]]   # x1, y1, x2, y2
				x1, y1, x2, y2 = coordinate
				if abs(x1) >= padding_threshold and abs(y1) >= padding_threshold and abs(im_width - abs(x2)) >= padding_threshold and abs(im_height - abs(y2)) >= padding_threshold:

					landmarks = [[max_bbox[2 * i - 1], max_bbox[2 * i]] for i in range(3, 8)]

					# Face Anti Spoofing
					# image_bbox: x_top_left, y_top_left, width, height
					image_bbox = [int(max_bbox[0]), int(max_bbox[1]),
								  int(max_bbox[2]-max_bbox[0]), int(max_bbox[3]-max_bbox[1])]
					spoof = detect_spoof(model_spoofing, image_bbox, original_img)

					# Get extract_feature
					warped_face2 = warp_and_crop_face(
						cv2.cvtColor(frame, cv2.COLOR_BGR2RGB), landmarks, reference, (112, 112))

					obj_base64string = codecs.encode(
						pickle.dumps(warped_face2, protocol=pickle.HIGHEST_PROTOCOL), "base64").decode('utf-8')
					# url = 'http://127.0.0.1:8000/recognition'
					url = 'http://42.114.166.123:14210/recognition'
					my_input = {'input':
							{'image': obj_base64string, 'use_base64': False, 'image_size': 112, 'threshold': 0.4}}
					result = requests.post(url, json=my_input)
					result = result.json()
					cosin = result['similarity']
					name = result['name']

					# Draw box
					draw_box(frame, coordinate, cosin, name, spoof)

					faces_queue.put({'label': name, 'spoof': spoof})

			else:
				faces_queue.put({'label': "None", 'spoof': 0})

			result_tracking = track_queue(faces_queue)
			current_state = check_change(result_tracking, current_state)

			if len(faces_queue.queue) >= 7:
				faces_queue.get()

			cv2.imshow('frame', frame)

			if cv2.waitKey(1) & 0xFF == ord('q'):
				break
			print('Time frame ', time.time() - start)
		count += 1
		if count > 10000:
		    count = 0
		    # break
	camera.release()
	cv2.destroyAllWindows()


if __name__ == '__main__':
    main(tensorrt=False, cam_device=None, input_size=(480, 640))
