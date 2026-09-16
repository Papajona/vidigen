import unittest
from PIL import Image
from gateway.reframe import bbox_from_alpha, compute_crop_box


class TestBboxFromAlpha(unittest.TestCase):
    def test_finds_subject_in_corner(self):
        img = Image.new('RGBA', (100, 100), (0, 0, 0, 0))
        for x in range(60, 90):
            for y in range(10, 40):
                img.putpixel((x, y), (255, 255, 255, 255))
        bbox = bbox_from_alpha(img)
        self.assertIsNotNone(bbox)
        left, top, right, bottom = bbox
        self.assertTrue(55 <= left <= 61)
        self.assertTrue(88 <= right <= 91)

    def test_empty_mask_returns_none(self):
        img = Image.new('RGBA', (100, 100), (0, 0, 0, 0))
        self.assertIsNone(bbox_from_alpha(img))

    def test_non_rgba_returns_none(self):
        img = Image.new('RGB', (100, 100), (255, 255, 255))
        self.assertIsNone(bbox_from_alpha(img))


class TestComputeCropBox(unittest.TestCase):
    def test_centers_on_subject_when_room_allows(self):
        # Subject dead-center in a huge frame -> crop should also be dead-center.
        box = compute_crop_box(frame_size=(1000, 1000), subject_bbox=(480, 480, 520, 520), target_aspect_w=9, target_aspect_h=16)
        x, y, w, h = box
        self.assertAlmostEqual(x + w / 2, 500, delta=2)
        self.assertAlmostEqual(y + h / 2, 500, delta=2)

    def test_clamps_when_subject_near_edge(self):
        # Subject hugging the left edge -> crop must not run off-frame to the left.
        box = compute_crop_box(frame_size=(1920, 1080), subject_bbox=(0, 400, 50, 600), target_aspect_w=9, target_aspect_h=16)
        x, y, w, h = box
        self.assertGreaterEqual(x, 0)
        self.assertLessEqual(x + w, 1920)

    def test_output_matches_target_aspect_ratio(self):
        box = compute_crop_box(frame_size=(1920, 1080), subject_bbox=(800, 400, 1100, 700), target_aspect_w=1, target_aspect_h=1)
        _, _, w, h = box
        self.assertAlmostEqual(w / h, 1.0, delta=0.02)

    def test_falls_back_to_frame_center_with_no_subject(self):
        box = compute_crop_box(frame_size=(1920, 1080), subject_bbox=None, target_aspect_w=9, target_aspect_h=16)
        x, y, w, h = box
        self.assertAlmostEqual(x + w / 2, 960, delta=2)

    def test_rejects_invalid_dimensions(self):
        with self.assertRaises(ValueError):
            compute_crop_box(frame_size=(0, 1080), subject_bbox=None, target_aspect_w=9, target_aspect_h=16)
        with self.assertRaises(ValueError):
            compute_crop_box(frame_size=(1920, 1080), subject_bbox=None, target_aspect_w=0, target_aspect_h=16)


if __name__ == '__main__':
    unittest.main()
