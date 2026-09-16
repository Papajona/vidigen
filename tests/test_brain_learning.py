import tempfile, unittest
from pathlib import Path
from PIL import Image
from gateway.brain_learning import _sample_image

class BrainLearningTests(unittest.TestCase):
    def test_image_sampling_extracts_stats(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'x.png'
            Image.new('RGB',(320,180),(120,80,40)).save(p)
            meta,samples=_sample_image(str(p),d)
            self.assertEqual(meta['media_type'],'image')
            self.assertEqual(meta['width'],320)
            self.assertEqual(len(samples),1)
            self.assertGreater(samples[0]['brightness'],0)

if __name__=='__main__': unittest.main()
