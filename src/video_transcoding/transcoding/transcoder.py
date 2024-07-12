import abc
import os.path
from itertools import product
from typing import List, Dict, Any, Optional
from urllib.parse import urljoin

from fffw import encoding
from fffw.encoding.vector import SIMD, Vector
from fffw.graph import VIDEO

from video_transcoding.transcoding import codecs, outputs
from video_transcoding.transcoding.metadata import Metadata
from video_transcoding.transcoding.profiles import Profile
from video_transcoding.transcoding.strategy import Processor


class FFMPEGProcessor(Processor, abc.ABC):
    """
    Base class for ffmpeg-based processing blocks.
    """

    def __init__(self, src: str, dst: str, *,
                 profile: Profile,
                 meta: Optional[Metadata] = None) -> None:
        super().__init__(src, dst)
        self.profile = profile
        self.meta = meta

    @staticmethod
    def run(ff: encoding.FFMPEG) -> None:
        """ Starts ffmpeg process and captures errors from it's logs"""
        return_code, output, error = ff.run()
        if return_code != 0:
            # Check return code and error messages
            error = error or f"invalid ffmpeg return code {return_code}"
            raise RuntimeError(error)

    def process(self) -> Metadata:
        if self.meta is None:
            self.meta = self.get_media_info(self.src)
        ff = self.prepare_ffmpeg(self.meta)
        self.run(ff)
        # Get result mediainfo
        dst = self.get_media_info(self.dst)
        return dst

    @abc.abstractmethod
    def prepare_ffmpeg(self, src: Metadata) -> encoding.FFMPEG:
        raise NotImplementedError


class Transcoder(FFMPEGProcessor):
    """
    Source transcoding logic.
    """

    def prepare_ffmpeg(self, src: Metadata) -> encoding.FFMPEG:
        """
        Prepares ffmpeg command for a given source
        :param src: input file metadata
        :return: ffmpeg wrapper
        """
        # Initialize source file descriptor with stream metadata
        source = self.prepare_input(src)

        # Initialize output file with audio and codecs from profile tracks.
        video_codecs = self.prepare_video_codecs()
        audio_codecs = self.prepre_audio_codecs()
        dst = self.prepare_output(audio_codecs, video_codecs)

        # ffmpeg wrapper with vectorized processing capabilities
        simd = SIMD(source, dst,
                    overwrite=True, loglevel='repeat+level+info')

        # per-video-track scaling
        scaling_params = [
            (video.width, video.height) for video in self.profile.video
        ]
        scaled_video = simd.video.connect(encoding.Scale, params=scaling_params)

        # connect scaled video streams to simd video codecs
        scaled_video | Vector(video_codecs)

        # pass audio as is to simd audio codecs
        simd.audio | Vector(audio_codecs)

        return simd.ffmpeg

    @staticmethod
    def prepare_input(src: Metadata) -> encoding.Input:
        return encoding.input_file(src.uri, *src.streams)

    def prepare_output(self,
                       audio_codecs: List[encoding.AudioCodec],
                       video_codecs: List[encoding.VideoCodec],
                       ) -> encoding.Output:
        return outputs.FileOutput(
            output_file=self.dst,
            method='PUT',
            codecs=[*video_codecs, *audio_codecs],
            format=self.profile.container.format
        )

    def prepre_audio_codecs(self) -> List[codecs.AudioCodec]:
        audio_codecs = []
        for audio in self.profile.audio:
            audio_codecs.append(codecs.AudioCodec(
                codec=audio.codec,
                bitrate=audio.bitrate,
                channels=audio.channels,
                rate=audio.sample_rate,
            ))
        return audio_codecs

    def prepare_video_codecs(self) -> List[codecs.VideoCodec]:
        video_codecs = []
        for video in self.profile.video:
            video_codecs.append(codecs.VideoCodec(
                codec=video.codec,
                force_key_frames=video.force_key_frames,
                constant_rate_factor=video.constant_rate_factor,
                preset=video.preset,
                max_rate=video.max_rate,
                buf_size=video.buf_size,
                profile=video.profile,
                pix_fmt=video.pix_fmt,
                gop=video.gop_size,
                rate=video.frame_rate,
            ))
        return video_codecs


class Splitter(FFMPEGProcessor):
    """
    Source splitting logic.
    """

    def prepare_ffmpeg(self, src: Metadata) -> encoding.FFMPEG:
        source = encoding.input_file(self.src, *src.streams)
        codecs_list = []
        for s in source.streams:
            codecs_list.append(s > encoding.Copy(kind=s.kind))
        out = self.prepare_output(codecs_list)
        return encoding.FFMPEG(input=source, output=out, loglevel='level+info')

    def prepare_output(self,
                       codecs_list: List[encoding.Codec]
                       ) -> encoding.Output:
        return outputs.HLSOutput(
            **self.get_output_kwargs(codecs_list)
        )

    def get_output_kwargs(self,
                          codecs_list: List[encoding.Codec]
                          ) -> Dict[str, Any]:
        return dict(
            output_file=self.dst,
            hls_time=self.profile.container.segment_duration,
            hls_playlist_type='vod',
            codecs=codecs_list,
        )


class HLSSegmentor(Splitter):
    """
    Result segmentation logic.
    """

    def get_output_kwargs(self,
                          codecs_list: List[encoding.Codec]
                          ) -> Dict[str, Any]:
        kwargs = super().get_output_kwargs(codecs_list)
        kwargs.update(
            output_file=urljoin(self.dst, 'playlist-%v.m3u8'),
            hls_segment_filename=urljoin(self.dst, 'segment-%v-%05d.ts'),
            master_pl_name=os.path.basename(self.dst),
            var_stream_map=self.get_var_stream_map(codecs_list)
        )
        return kwargs

    @staticmethod
    def get_var_stream_map(codecs_list: List[encoding.Codec]) -> str:
        audios = []
        videos = []
        for c in codecs_list:
            if c.kind == VIDEO:
                videos.append(c)
            else:
                audios.append(c)
        vsm = []
        for i, a in enumerate(audios):
            vsm.append(f'a:{i},agroup:a{i}')
        for (i, a), (j, v) in product(enumerate(audios), enumerate(videos)):
            vsm.append(f'v:{j},agroup:a{i}')
        var_stream_map = ' '.join(vsm)
        return var_stream_map
