# Vidigen Resource Learning Policy

Vidigen may learn from **user-provided or appropriately licensed** resources such as:

- ComfyUI workflows and node examples
- open-source code repositories
- model documentation
- text-to-image examples
- text-to-video workflows
- photo-editing workflows
- user-owned sample images and videos

For every imported resource, keep:

- source URL/repository
- author/owner where known
- license name and URL
- retrieval date
- local checksum where practical
- permitted-use notes

Do not automatically scrape arbitrary websites or download copyrighted media for training. Do not bypass paywalls, access controls, robots restrictions, or copyright-management systems.

The first learning layer in V7 learns **workflow preferences and request patterns**. A future dataset/fine-tuning pipeline should be separate, auditable, GPU-backed, and license-aware.
