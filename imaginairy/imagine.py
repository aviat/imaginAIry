import logging
import os
import re
import subprocess
from contextlib import nullcontext
from functools import lru_cache

import PIL
import numpy as np
import torch
from PIL import Image
from einops import rearrange
from omegaconf import OmegaConf
from pytorch_lightning import seed_everything
from torch import autocast
from transformers import cached_path

from imaginairy.models.diffusion.ddim import DDIMSampler
from imaginairy.models.diffusion.plms import PLMSSampler
from imaginairy.schema import ImaginePrompt, ImagineResult
from imaginairy.utils import (
    get_device,
    instantiate_from_config,
    fix_torch_nn_layer_norm,
)

# from diffusers.pipelines.stable_diffusion.safety_checker import StableDiffusionSafetyChecker
# from transformers import AutoFeatureExtractor

# load safety model
# safety_model_id = "CompVis/stable-diffusion-safety-checker"
# safety_feature_extractor = AutoFeatureExtractor.from_pretrained(safety_model_id)
# safety_checker = StableDiffusionSafetyChecker.from_pretrained(safety_model_id)

LIB_PATH = os.path.dirname(__file__)
logger = logging.getLogger(__name__)


def load_model_from_config(config):
    ckpt_path = cached_path(
        "https://www.googleapis.com/storage/v1/b/aai-blog-files/o/sd-v1-4.ckpt?alt=media"
    )
    logger.info(f"Loading model from {ckpt_path}")
    pl_sd = torch.load(ckpt_path, map_location="cpu")
    if "global_step" in pl_sd:
        logger.info(f"Global Step: {pl_sd['global_step']}")
    sd = pl_sd["state_dict"]
    model = instantiate_from_config(config.model)
    m, u = model.load_state_dict(sd, strict=False)
    if len(m) > 0:
        logger.info(f"missing keys: {m}")
    if len(u) > 0:
        logger.info(f"unexpected keys: {u}")

    model.to(get_device())
    model.eval()
    return model


def load_img(path, max_height=512, max_width=512):
    image = Image.open(path).convert("RGB")
    w, h = image.size
    logger.info(f"loaded input image of size ({w}, {h}) from {path}")
    resize_ratio = min(max_width / w, max_height / h)
    w, h = int(w * resize_ratio), int(h * resize_ratio)
    w, h = map(lambda x: x - x % 64, (w, h))  # resize to integer multiple of 32
    image = image.resize((w, h), resample=PIL.Image.LANCZOS)
    image = np.array(image).astype(np.float32) / 255.0
    image = image[None].transpose(0, 3, 1, 2)
    image = torch.from_numpy(image)
    return 2.0 * image - 1.0, w, h


@lru_cache()
def load_model():
    config = "configs/stable-diffusion-v1.yaml"
    config = OmegaConf.load(f"{LIB_PATH}/{config}")
    model = load_model_from_config(config)

    model = model.to(get_device())
    return model


def imagine_image_files(
    prompts,
    outdir,
    latent_channels=4,
    downsampling_factor=8,
    precision="autocast",
    ddim_eta=0.0,
    record_steps=False,
):
    big_path = os.path.join(outdir, "upscaled")
    os.makedirs(outdir, exist_ok=True)
    os.makedirs(big_path, exist_ok=True)
    base_count = len(os.listdir(outdir))
    step_count = 0

    def _record_steps(samples, i, model, prompt):
        nonlocal step_count
        step_count += 1
        samples = model.decode_first_stage(samples)
        samples = torch.clamp((samples + 1.0) / 2.0, min=0.0, max=1.0)
        steps_path = os.path.join(outdir, "steps", f"{base_count:08}_S{prompt.seed}")
        os.makedirs(steps_path, exist_ok=True)
        for pred_x0 in samples:
            pred_x0 = 255.0 * rearrange(pred_x0.cpu().numpy(), "c h w -> h w c")
            filename = f"{base_count:08}_S{prompt.seed}_step{step_count:04}.jpg"
            Image.fromarray(pred_x0.astype(np.uint8)).save(
                os.path.join(steps_path, filename)
            )

    img_callback = _record_steps if record_steps else None
    for result in imagine_images(
        prompts,
        latent_channels=latent_channels,
        downsampling_factor=downsampling_factor,
        precision=precision,
        ddim_eta=ddim_eta,
        img_callback=img_callback,
    ):
        prompt = result.prompt
        img = result.img
        basefilename = f"{base_count:06}_{prompt.seed}_{prompt.sampler_type}{prompt.steps}_PS{prompt.prompt_strength}_{prompt_normalized(prompt.prompt_text)}"
        filepath = os.path.join(outdir, f"{basefilename}.jpg")

        img.save(filepath)
        if prompt.upscale:
            enlarge_realesrgan2x(
                filepath,
                os.path.join(big_path, basefilename) + ".jpg",
            )
        base_count += 1


def imagine_images(
    prompts,
    latent_channels=4,
    downsampling_factor=8,
    precision="autocast",
    ddim_eta=0.0,
    img_callback=None,
):
    model = load_model()
    prompts = [ImaginePrompt(prompts)] if isinstance(prompts, str) else prompts
    prompts = [prompts] if isinstance(prompts, ImaginePrompt) else prompts
    _img_callback = None

    precision_scope = autocast if precision == "autocast" else nullcontext
    with (torch.no_grad(), precision_scope("cuda"), fix_torch_nn_layer_norm()):
        for prompt in prompts:
            seed_everything(prompt.seed)
            uc = None
            if prompt.prompt_strength != 1.0:
                uc = model.get_learned_conditioning(1 * [""])
            total_weight = sum(wp.weight for wp in prompt.prompts)
            c = sum(
                [
                    model.get_learned_conditioning(wp.text) * (wp.weight / total_weight)
                    for wp in prompt.prompts
                ]
            )
            if img_callback:

                def _img_callback(samples, i):
                    img_callback(samples, i, model, prompt)

            shape = [
                latent_channels,
                prompt.height // downsampling_factor,
                prompt.width // downsampling_factor,
            ]

            start_code = None
            sampler = get_sampler(prompt.sampler_type, model)
            if prompt.init_image:
                generation_strength = 1 - prompt.init_image_strength
                ddim_steps = int(prompt.steps / generation_strength)
                sampler.make_schedule(ddim_num_steps=ddim_steps, ddim_eta=ddim_eta)

                t_enc = int(generation_strength * ddim_steps)
                init_image, w, h = load_img(prompt.init_image)
                init_image = init_image.to(get_device())
                init_latent = model.encode_first_stage(init_image)
                noised_init_latent = model.get_first_stage_encoding(init_latent)
                _img_callback(init_latent.mean, 0)
                _img_callback(noised_init_latent, 0)

                # encode (scaled latent)
                z_enc = sampler.stochastic_encode(
                    noised_init_latent,
                    torch.tensor([t_enc]).to(get_device()),
                )
                _img_callback(noised_init_latent, 0)

                # decode it
                samples = sampler.decode(
                    z_enc,
                    c,
                    t_enc,
                    unconditional_guidance_scale=prompt.prompt_strength,
                    unconditional_conditioning=uc,
                    img_callback=_img_callback,
                )
            else:

                samples, _ = sampler.sample(
                    S=prompt.steps,
                    conditioning=c,
                    batch_size=1,
                    shape=shape,
                    unconditional_guidance_scale=prompt.prompt_strength,
                    unconditional_conditioning=uc,
                    eta=ddim_eta,
                    x_T=start_code,
                    img_callback=_img_callback,
                )

            x_samples = model.decode_first_stage(samples)
            x_samples = torch.clamp((x_samples + 1.0) / 2.0, min=0.0, max=1.0)

            for x_sample in x_samples:
                x_sample = 255.0 * rearrange(x_sample.cpu().numpy(), "c h w -> h w c")
                img = Image.fromarray(x_sample.astype(np.uint8))
                if prompt.fix_faces:
                    img = fix_faces_GFPGAN(img)
                # if prompt.upscale:
                #     enlarge_realesrgan2x(
                #         filepath,
                #         os.path.join(big_path, basefilename) + ".jpg",
                #     )
                yield ImagineResult(img=img, prompt=prompt)


def prompt_normalized(prompt):
    return re.sub(r"[^a-zA-Z0-9.,]+", "_", prompt)[:130]


DOWNLOADED_FILES_PATH = f"{LIB_PATH}/../downloads/"
ESRGAN_PATH = DOWNLOADED_FILES_PATH + "realesrgan-ncnn-vulkan/realesrgan-ncnn-vulkan"


def enlarge_realesrgan2x(src, dst):
    process = subprocess.Popen(
        [ESRGAN_PATH, "-i", src, "-o", dst, "-n", "realesrgan-x4plus"]
    )
    process.wait()


def get_sampler(sampler_type, model):
    sampler_type = sampler_type.upper()
    if sampler_type == "PLMS":
        return PLMSSampler(model)
    elif sampler_type == "DDIM":
        return DDIMSampler(model)


def gfpgan_model():
    from gfpgan import GFPGANer

    return GFPGANer(
        model_path=DOWNLOADED_FILES_PATH
        + "GFPGAN/experiments/pretrained_models/GFPGANv1.3.pth",
        upscale=1,
        arch="clean",
        channel_multiplier=2,
        bg_upsampler=None,
        device=torch.device(get_device()),
    )


def fix_faces_GFPGAN(image):
    image = image.convert("RGB")
    cropped_faces, restored_faces, restored_img = gfpgan_model().enhance(
        np.array(image, dtype=np.uint8),
        has_aligned=False,
        only_center_face=False,
        paste_back=True,
    )
    res = Image.fromarray(restored_img)

    return res
