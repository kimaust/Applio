import librosa
import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
from scipy.signal import savgol_filter
from scipy.ndimage import label

from librosa.filters import mel
from typing import List

import torbi

N_MELS = 128
N_CLASS = 360


class ConvBlockRes(nn.Module):
    """
    A convolutional block with residual connection.

    Args:
        in_channels (int): Number of input channels.
        out_channels (int): Number of output channels.
        momentum (float): Momentum for batch normalization.
    """

    def __init__(self, in_channels, out_channels, momentum=0.01):
        super(ConvBlockRes, self).__init__()
        self.conv = nn.Sequential(
            nn.Conv2d(
                in_channels=in_channels,
                out_channels=out_channels,
                kernel_size=(3, 3),
                stride=(1, 1),
                padding=(1, 1),
                bias=False,
            ),
            nn.BatchNorm2d(out_channels, momentum=momentum),
            nn.ReLU(),
            nn.Conv2d(
                in_channels=out_channels,
                out_channels=out_channels,
                kernel_size=(3, 3),
                stride=(1, 1),
                padding=(1, 1),
                bias=False,
            ),
            nn.BatchNorm2d(out_channels, momentum=momentum),
            nn.ReLU(),
        )
        if in_channels != out_channels:
            self.shortcut = nn.Conv2d(in_channels, out_channels, (1, 1))
            self.is_shortcut = True
        else:
            self.is_shortcut = False

    def forward(self, x):
        if self.is_shortcut:
            return self.conv(x) + self.shortcut(x)
        else:
            return self.conv(x) + x


class ResEncoderBlock(nn.Module):
    """
    A residual encoder block.

    Args:
        in_channels (int): Number of input channels.
        out_channels (int): Number of output channels.
        kernel_size (tuple): Size of the average pooling kernel.
        n_blocks (int): Number of convolutional blocks in the block.
        momentum (float): Momentum for batch normalization.
    """

    def __init__(
        self, in_channels, out_channels, kernel_size, n_blocks=1, momentum=0.01
    ):
        super(ResEncoderBlock, self).__init__()
        self.n_blocks = n_blocks
        self.conv = nn.ModuleList()
        self.conv.append(ConvBlockRes(in_channels, out_channels, momentum))
        for _ in range(n_blocks - 1):
            self.conv.append(ConvBlockRes(
                out_channels, out_channels, momentum))
        self.kernel_size = kernel_size
        if self.kernel_size is not None:
            self.pool = nn.AvgPool2d(kernel_size=kernel_size)

    def forward(self, x):
        for i in range(self.n_blocks):
            x = self.conv[i](x)
        if self.kernel_size is not None:
            return x, self.pool(x)
        else:
            return x


class Encoder(nn.Module):
    """
    The encoder part of the DeepUnet.

    Args:
        in_channels (int): Number of input channels.
        in_size (int): Size of the input tensor.
        n_encoders (int): Number of encoder blocks.
        kernel_size (tuple): Size of the average pooling kernel.
        n_blocks (int): Number of convolutional blocks in each encoder block.
        out_channels (int): Number of output channels for the first encoder block.
        momentum (float): Momentum for batch normalization.
    """

    def __init__(
        self,
        in_channels,
        in_size,
        n_encoders,
        kernel_size,
        n_blocks,
        out_channels=16,
        momentum=0.01,
    ):
        super(Encoder, self).__init__()
        self.n_encoders = n_encoders
        self.bn = nn.BatchNorm2d(in_channels, momentum=momentum)
        self.layers = nn.ModuleList()
        self.latent_channels = []
        for i in range(self.n_encoders):
            self.layers.append(
                ResEncoderBlock(
                    in_channels, out_channels, kernel_size, n_blocks, momentum=momentum
                )
            )
            self.latent_channels.append([out_channels, in_size])
            in_channels = out_channels
            out_channels *= 2
            in_size //= 2
        self.out_size = in_size
        self.out_channel = out_channels

    def forward(self, x: torch.Tensor):
        concat_tensors: List[torch.Tensor] = []
        x = self.bn(x)
        for i in range(self.n_encoders):
            t, x = self.layers[i](x)
            concat_tensors.append(t)
        return x, concat_tensors


class Intermediate(nn.Module):
    """
    The intermediate layer of the DeepUnet.

    Args:
        in_channels (int): Number of input channels.
        out_channels (int): Number of output channels.
        n_inters (int): Number of convolutional blocks in the intermediate layer.
        n_blocks (int): Number of convolutional blocks in each intermediate block.
        momentum (float): Momentum for batch normalization.
    """

    def __init__(self, in_channels, out_channels, n_inters, n_blocks, momentum=0.01):
        super(Intermediate, self).__init__()
        self.n_inters = n_inters
        self.layers = nn.ModuleList()
        self.layers.append(
            ResEncoderBlock(in_channels, out_channels,
                            None, n_blocks, momentum)
        )
        for _ in range(self.n_inters - 1):
            self.layers.append(
                ResEncoderBlock(out_channels, out_channels,
                                None, n_blocks, momentum)
            )

    def forward(self, x):
        for i in range(self.n_inters):
            x = self.layers[i](x)
        return x


class ResDecoderBlock(nn.Module):
    """
    A residual decoder block.

    Args:
        in_channels (int): Number of input channels.
        out_channels (int): Number of output channels.
        stride (tuple): Stride for transposed convolution.
        n_blocks (int): Number of convolutional blocks in the block.
        momentum (float): Momentum for batch normalization.
    """

    def __init__(self, in_channels, out_channels, stride, n_blocks=1, momentum=0.01):
        super(ResDecoderBlock, self).__init__()
        out_padding = (0, 1) if stride == (1, 2) else (1, 1)
        self.n_blocks = n_blocks
        self.conv1 = nn.Sequential(
            nn.ConvTranspose2d(
                in_channels=in_channels,
                out_channels=out_channels,
                kernel_size=(3, 3),
                stride=stride,
                padding=(1, 1),
                output_padding=out_padding,
                bias=False,
            ),
            nn.BatchNorm2d(out_channels, momentum=momentum),
            nn.ReLU(),
        )
        self.conv2 = nn.ModuleList()
        self.conv2.append(ConvBlockRes(
            out_channels * 2, out_channels, momentum))
        for _ in range(n_blocks - 1):
            self.conv2.append(ConvBlockRes(
                out_channels, out_channels, momentum))

    def forward(self, x, concat_tensor):
        x = self.conv1(x)
        x = torch.cat((x, concat_tensor), dim=1)
        for i in range(self.n_blocks):
            x = self.conv2[i](x)
        return x


class Decoder(nn.Module):
    """
    The decoder part of the DeepUnet.

    Args:
        in_channels (int): Number of input channels.
        n_decoders (int): Number of decoder blocks.
        stride (tuple): Stride for transposed convolution.
        n_blocks (int): Number of convolutional blocks in each decoder block.
        momentum (float): Momentum for batch normalization.
    """

    def __init__(self, in_channels, n_decoders, stride, n_blocks, momentum=0.01):
        super(Decoder, self).__init__()
        self.layers = nn.ModuleList()
        self.n_decoders = n_decoders
        for _ in range(self.n_decoders):
            out_channels = in_channels // 2
            self.layers.append(
                ResDecoderBlock(in_channels, out_channels,
                                stride, n_blocks, momentum)
            )
            in_channels = out_channels

    def forward(self, x, concat_tensors):
        for i in range(self.n_decoders):
            x = self.layers[i](x, concat_tensors[-1 - i])
        return x


class DeepUnet(nn.Module):
    """
    The DeepUnet architecture.

    Args:
        kernel_size (tuple): Size of the average pooling kernel.
        n_blocks (int): Number of convolutional blocks in each encoder/decoder block.
        en_de_layers (int): Number of encoder/decoder layers.
        inter_layers (int): Number of convolutional blocks in the intermediate layer.
        in_channels (int): Number of input channels.
        en_out_channels (int): Number of output channels for the first encoder block.
    """

    def __init__(
        self,
        kernel_size,
        n_blocks,
        en_de_layers=5,
        inter_layers=4,
        in_channels=1,
        en_out_channels=16,
    ):
        super(DeepUnet, self).__init__()
        self.encoder = Encoder(
            in_channels, 128, en_de_layers, kernel_size, n_blocks, en_out_channels
        )
        self.intermediate = Intermediate(
            self.encoder.out_channel // 2,
            self.encoder.out_channel,
            inter_layers,
            n_blocks,
        )
        self.decoder = Decoder(
            self.encoder.out_channel, en_de_layers, kernel_size, n_blocks
        )

    def forward(self, x):
        x, concat_tensors = self.encoder(x)
        x = self.intermediate(x)
        x = self.decoder(x, concat_tensors)
        return x


class E2E(nn.Module):
    """
    The end-to-end model.

    Args:
        n_blocks (int): Number of convolutional blocks in each encoder/decoder block.
        n_gru (int): Number of GRU layers.
        kernel_size (tuple): Size of the average pooling kernel.
        en_de_layers (int): Number of encoder/decoder layers.
        inter_layers (int): Number of convolutional blocks in the intermediate layer.
        in_channels (int): Number of input channels.
        en_out_channels (int): Number of output channels for the first encoder block.
    """

    def __init__(
        self,
        n_blocks,
        n_gru,
        kernel_size,
        en_de_layers=5,
        inter_layers=4,
        in_channels=1,
        en_out_channels=16,
    ):
        super(E2E, self).__init__()
        self.unet = DeepUnet(
            kernel_size,
            n_blocks,
            en_de_layers,
            inter_layers,
            in_channels,
            en_out_channels,
        )
        self.cnn = nn.Conv2d(en_out_channels, 3, (3, 3), padding=(1, 1))
        if n_gru:
            self.fc = nn.Sequential(
                BiGRU(3 * 128, 256, n_gru),
                nn.Linear(512, N_CLASS),
                nn.Dropout(0.25),
                nn.Sigmoid(),
            )
        else:
            self.fc = nn.Sequential(
                nn.Linear(3 * N_MELS, N_CLASS), nn.Dropout(0.25), nn.Sigmoid()
            )

    def forward(self, mel):
        mel = mel.transpose(-1, -2).unsqueeze(1)
        x = self.cnn(self.unet(mel)).transpose(1, 2).flatten(-2)
        x = self.fc(x)
        return x


class MelSpectrogram(torch.nn.Module):
    """
    Extracts Mel-spectrogram features from audio.

    Args:
        n_mel_channels (int): Number of Mel-frequency bands.
        sample_rate (int): Sampling rate of the audio.
        win_length (int): Length of the window function in samples.
        hop_length (int): Hop size between frames in samples.
        n_fft (int, optional): Length of the FFT window. Defaults to None, which uses win_length.
        mel_fmin (int, optional): Minimum frequency for the Mel filter bank. Defaults to 0.
        mel_fmax (int, optional): Maximum frequency for the Mel filter bank. Defaults to None.
        clamp (float, optional): Minimum value for clamping the Mel-spectrogram. Defaults to 1e-5.
    """

    def __init__(
        self,
        n_mel_channels,
        sample_rate,
        win_length,
        hop_length,
        n_fft=None,
        mel_fmin=0,
        mel_fmax=None,
        clamp=1e-5,
    ):
        super().__init__()
        n_fft = win_length if n_fft is None else n_fft
        self.hann_window = {}
        mel_basis = mel(
            sr=sample_rate,
            n_fft=n_fft,
            n_mels=n_mel_channels,
            fmin=mel_fmin,
            fmax=mel_fmax,
            htk=True,
        )
        mel_basis = torch.from_numpy(mel_basis).float()
        self.register_buffer("mel_basis", mel_basis)
        self.n_fft = win_length if n_fft is None else n_fft
        self.hop_length = hop_length
        self.win_length = win_length
        self.sample_rate = sample_rate
        self.n_mel_channels = n_mel_channels
        self.clamp = clamp

    def forward(self, audio, keyshift=0, speed=1, center=True):
        factor = 2 ** (keyshift / 12)
        n_fft_new = int(np.round(self.n_fft * factor))
        win_length_new = int(np.round(self.win_length * factor))
        hop_length_new = int(np.round(self.hop_length * speed))
        keyshift_key = str(keyshift) + "_" + str(audio.device)
        if keyshift_key not in self.hann_window:
            self.hann_window[keyshift_key] = torch.hann_window(win_length_new).to(
                audio.device
            )
        fft = torch.stft(
            audio,
            n_fft=n_fft_new,
            hop_length=hop_length_new,
            win_length=win_length_new,
            window=self.hann_window[keyshift_key],
            center=center,
            return_complex=True,
        )

        magnitude = torch.sqrt(fft.real.pow(2) + fft.imag.pow(2))
        if keyshift != 0:
            size = self.n_fft // 2 + 1
            resize = magnitude.size(1)
            if resize < size:
                magnitude = F.pad(magnitude, (0, 0, 0, size - resize))
            magnitude = magnitude[:, :size, :] * \
                self.win_length / win_length_new
        mel_output = torch.matmul(self.mel_basis, magnitude)
        log_mel_spec = torch.log(torch.clamp(mel_output, min=self.clamp))
        return log_mel_spec


class RMVPE0PredictorV2:
    """
    A predictor for fundamental frequency (F0) based on the RMVPE0 model.

    Args:
        model_path (str): Path to the RMVPE0 model file.
        device (str, optional): Device to use for computation. Defaults to None, which uses CUDA if available.
    """
    PRIMES = np.array([1, 3, 5, 7, 11, 13])
    SHIFT = np.round(60 * np.log2(PRIMES)).astype(int)
    WEIGHTS = 1 / PRIMES

    def __init__(self, model_path, device=None):
        self.resample_kernel = {}
        model = E2E(4, 1, (2, 2))
        ckpt = torch.load(model_path, map_location="cpu", weights_only=True)
        model.load_state_dict(ckpt)
        model.eval()
        self.model = model
        self.resample_kernel = {}
        self.device = device

        self.shifts_int = self.SHIFT.tolist()
        self.weight_torch = torch.from_numpy(self.WEIGHTS).float().to(self.device)
        self.mel_extractor = MelSpectrogram(
            N_MELS, 16000, 1024, 160, None, 30, 8000
        ).to(device)
        self.model = self.model.to(device)
        cents_mapping = 20 * np.arange(N_CLASS) + 1997.3794084376191
        self.cents_mapping = np.pad(cents_mapping, (4, 4))

        self._trans = librosa.sequence.transition_local(N_CLASS, width=20, wrap=True)

    def mel2hidden(self, mel):
        """
        Converts Mel-spectrogram features to hidden representation.

        Args:
            mel (torch.Tensor): Mel-spectrogram features.
        """
        with torch.no_grad():
            n_frames = mel.shape[-1]
            mel = F.pad(
                mel, (0, 32 * ((n_frames - 1) // 32 + 1) - n_frames), mode="reflect"
            )
            hidden = self.model(mel)
            return hidden[:, :n_frames]

    def decode(self, hidden, thred=0.03):
        eps = 1e-12

        hidden_t = torch.from_numpy(hidden).to(self.device)
        probs_t = self.prime_harmonic_sum(hidden_t)
        probs = probs_t.cpu().numpy()
        max_sal = probs.max(axis=1)
        voiced_mask = max_sal > thred

        # sharpness
        # probs = probs * 8

        row_sums = probs.sum(axis=1, keepdims=True)
        probs = probs / np.maximum(row_sums, eps)
        probs = probs.T  # (360, n_frames)

        # path = librosa.sequence.viterbi(probs, trans)  # (n_frames,)
        # path = librosa.sequence.viterbi_discriminative(probs, self._trans)
        path = self._viterbi_torbi_decode(probs, self._trans)

        cents_pred = self.cents_mapping[path + 4]
        f0 = 10 * (2 ** (cents_pred / 1200))
        f0[f0 == 10] = 0
        f0[~voiced_mask] = 0.0

        f0 = self.smooth_f0_savgol_segments(f0)
        f0 = self.despike_morph_zero(f0)
        return f0

    def infer_from_audio(self, audio, thred=0.03):
        """
        Infers F0 from audio.

        Args:
            audio (np.ndarray): Audio signal.
            thred (float, optional): Threshold for salience. Defaults to 0.03.
        """
        audio = torch.from_numpy(audio).float().to(self.device).unsqueeze(0)
        mel = self.mel_extractor(audio, center=True)
        hidden = self.mel2hidden(mel)
        hidden = hidden.squeeze(0).cpu().numpy()
        f0 = self.decode(hidden, thred=thred)
        return f0

    def smooth_f0_savgol_segments(
            self, f0,
            frame_rate=100,
            win_ms=50,
            poly=2):
        f0_out = f0.copy()
        voiced = f0 > 0
        if not voiced.any():
            return f0_out

        win = int(round((win_ms / 1000) * frame_rate)) | 1
        idx = np.flatnonzero(voiced)

        run_starts = np.where(np.diff(idx) > 1)[0] + 1
        runs = np.split(idx, run_starts)

        for run in runs:
            if len(run) < poly + 2:
                continue
            w = min(win, (len(run) | 1))
            logf = np.log2(f0[run])
            smoothed = savgol_filter(logf, w, poly, mode='nearest')
            f0_out[run] = 2 ** smoothed

        return f0_out

    def despike_morph_zero(self, f0, min_len=2):
        out = f0.copy()
        voiced_mask = f0 > 0

        labels, n_runs = label(voiced_mask)

        for lab in range(1, n_runs + 1):
            idx = np.where(labels == lab)[0]
            if len(idx) < min_len:
                out[idx] = 0.0

        return out

    def prime_harmonic_sum(self, hidden):
        agg = self.weight_torch[0] * hidden
        for s, weight in zip(self.shifts_int[1:], self.weight_torch[1:]):
            if s < hidden.shape[1]:
                agg[:, :-s] += weight * hidden[:, s:]

        return agg

    def _viterbi_torbi_decode(self, probs: np.ndarray, trans: np.ndarray):
        obs_t = torch.from_numpy(probs.T).float().to(self.device).unsqueeze(0)

        trans_t = torch.from_numpy(trans).float().to(self.device)
        init_t = torch.full((N_CLASS,), 1.0 / N_CLASS, dtype=torch.float32, device=self.device)

        gpu_id = 0 if self.device is not None and self.device.split(":")[0] == "cuda" else None

        path_t = torbi.from_probabilities(
            observation=obs_t,
            # batch_frames=torch.full((1,), seq_len, dtype=torch.int32, device=self.device),
            transition=trans_t,
            initial=init_t,
            log_probs=False,
            gpu=gpu_id,
        )

        return path_t[0].cpu().numpy()


class BiGRU(nn.Module):
    """
    A bidirectional GRU layer.

    Args:
        input_features (int): Number of input features.
        hidden_features (int): Number of hidden features.
        num_layers (int): Number of GRU layers.
    """

    def __init__(self, input_features, hidden_features, num_layers):
        super(BiGRU, self).__init__()
        self.gru = nn.GRU(
            input_features,
            hidden_features,
            num_layers=num_layers,
            batch_first=True,
            bidirectional=True,
        )

    def forward(self, x):
        return self.gru(x)[0]

