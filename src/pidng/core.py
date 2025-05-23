import warnings
import os
import numpy as np
import types
from .dng import Tag, dngIFD, dngTag, DNG, DNGTags
from .defs import Compression, DNGVersion, SampleFormat
from .packing import *
from .camdefs import BaseCameraModel

class DNGBASE:
    def __init__(self) -> None:
        self.compress = None
        self.path = None
        self.tags = None
        self.filter = None

    def __data_condition__(self, data : np.ndarray)  -> None:
        if data.dtype != np.uint16 and data.dtype != np.float32:
            raise Exception("RAW Data is not in correct format. Must be uint16_t or float32_t Numpy Array. ")

    def __tags_condition__(self, tags : DNGTags)  -> None:
        if not tags.get(Tag.ImageWidth):
            raise Exception("No width is defined in tags.")
        if not tags.get(Tag.ImageLength):
            raise Exception("No height is defined in tags.")
        if not tags.get(Tag.BitsPerSample):
            raise Exception("Bit per pixel is not defined.")     

    def __unpack_pixels__(self, data : np.ndarray) -> np.ndarray:
        return data   

    def __filter__(self, rawFrame: np.ndarray, filter : types.FunctionType) -> np.ndarray:

        if not filter:
            return rawFrame

        processed = filter(rawFrame)
        if not isinstance(processed, np.ndarray):
            raise TypeError("return value is not a valid numpy array!")
        elif processed.shape != rawFrame.shape:
            raise ValueError("return array does not have the same shape!")
        if processed.dtype != np.uint16:
            raise ValueError("array data type is invalid!")

        return processed


    def __process__(self, rawFrame : np.ndarray, tags: DNGTags, compress : bool, predictor: int = 6) -> bytearray:

        width = tags.get(Tag.ImageWidth).rawValue[0]
        length = tags.get(Tag.ImageLength).rawValue[0]
        bpp = tags.get(Tag.BitsPerSample).rawValue[0]

        compression_scheme = Compression.LJ92 if compress else Compression.Uncompressed

        sample_format = SampleFormat.Uint
        backward_version = DNGVersion.V1_0
        if rawFrame.dtype == np.float32:
            sample_format = SampleFormat.FloatingPoint
            # Floating-point data requires DNG 1.4
            backward_version = DNGVersion.V1_4
            # Floating-point data has to be compressed with deflate
            if compress:
                raise Exception("Compression is not supported for floating-point data")

        if compress:
            from ljpegCompress import pack16tolj

            if predictor == 1:
                tile = pack16tolj(rawFrame, int(width),
                                  int(length), bpp, 0, 0, 0, "", 1)
            elif predictor == 6:
                tile = pack16tolj(rawFrame, int(width*2),
                                  int(length/2), bpp, 0, 0, 0, "", 6)
            else:
                raise Exception("Predictor must be either 1 or 6")

        else:
            if bpp == 8:
                tile = rawFrame.astype('uint8').tobytes()
            elif bpp == 10:
                tile = pack10(rawFrame).tobytes()
            elif bpp == 12:
                tile = pack12(rawFrame).tobytes()
            elif bpp == 14:
                tile = pack14(rawFrame).tobytes()
            else:
                # 16-bit integers or 32-bit floats
                tile = rawFrame.tobytes()
        
        dngTemplate = DNG()

        dngTemplate.ImageDataStrips.append(tile)
        # set up the FULL IFD
        mainIFD = dngIFD()
        mainTagStripOffset = dngTag(
            Tag.StripOffsets, [0 for tile in dngTemplate.ImageDataStrips])
        mainIFD.tags.append(mainTagStripOffset)
        mainIFD.tags.append(dngTag(Tag.NewSubfileType, [0]))
        mainIFD.tags.append(dngTag(Tag.StripByteCounts, [len(
            tile) for tile in dngTemplate.ImageDataStrips]))
        mainIFD.tags.append(dngTag(Tag.Compression, [compression_scheme]))
        mainIFD.tags.append(dngTag(Tag.Software, "PiDNG"))
        mainIFD.tags.append(dngTag(Tag.DNGVersion, DNGVersion.V1_4))
        mainIFD.tags.append(dngTag(Tag.DNGBackwardVersion, backward_version))
        mainIFD.tags.append(dngTag(Tag.SampleFormat, [sample_format]))

        for tag in tags.list():
            try:
                mainIFD.tags.append(tag)
            except Exception as e:
                print("TAG Encoding Error!", e, tag)

        dngTemplate.IFDs.append(mainIFD)

        totalLength = dngTemplate.dataLen()

        mainTagStripOffset.setValue(
            [k for offset, k in dngTemplate.StripOffsets.items()])

        buf = bytearray(totalLength)
        dngTemplate.setBuffer(buf)
        dngTemplate.write()

        return buf

    def options(self, tags : DNGTags, path : str, compress=False, predictor=6) -> None:
        self.__tags_condition__(tags)
        self.tags = tags
        self.compress = compress
        self.path = path
        self.predictor = predictor

    def convert(self, image : np.ndarray, filename=""):

        if self.tags is None:
            raise Exception("Options have not been set!")
        
        # valdify incoming data
        self.__data_condition__(image)
        unpacked = self.__unpack_pixels__(image)
        filtered = self.__filter__(unpacked, self.filter)
        buf = self.__process__(filtered, self.tags, self.compress, self.predictor)

        file_output = False
        if len(filename) > 0:
            file_output = True

        if file_output:
            if not filename.endswith(".dng"):
                filename = filename + '.dng'
            outputDNG = os.path.join(self.path, filename)
            with open(outputDNG, "wb") as outfile:
                outfile.write(buf)
            return outputDNG
        else:
            return buf


class RAW2DNG(DNGBASE):
    def __init__(self) -> None:
        super().__init__()


class CAM2DNG(DNGBASE):
    def __init__(self, model : BaseCameraModel) -> None:
        super().__init__()
        self.model = model

    def options(self, path : str, compress=False) -> None:
        self.__tags_condition__(self.model.tags)
        self.tags = self.model.tags
        self.compress = compress
        self.path = path


class RPICAM2DNG(CAM2DNG):
    def __data_condition__(self, data : np.ndarray)  -> None:
        if data.dtype != np.uint8:
            warnings.warn("RAW Data is not in correct format. Already unpacked? ")

    def __unpack_pixels__(self, data : np.ndarray) -> np.ndarray:

        if data.dtype != np.uint8:
            return data

        width, height = self.model.fmt.get("size", (0,0))
        stride = self.model.fmt.get("stride", 0)
        bpp = self.model.fmt.get("bpp", 8)

        # check to see if stored packed or unpacked format
        if "CSI2P" in self.model.fmt.get("format", ""):
            s_bpp = bpp         # stored_bitperpixel
        else:
            s_bpp = 16

        bytes_per_row = int(width * (s_bpp / 8))
        data = data[:height, :bytes_per_row]

        if s_bpp == 10:
            data = data.astype(np.uint16) << 2
            for byte in range(4):
                data[:, byte::5] |= ((data[:, 4::5] >> ((byte+1) * 2)) & 0b11)
            data = np.delete(data, np.s_[4::5], 1)
        elif s_bpp == 12:
            data = data.astype(np.uint16)
            shape = data.shape
            unpacked_data = np.zeros((shape[0], int(shape[1] / 3 * 2)), dtype=np.uint16)
            unpacked_data[:, ::2] = (data[:, ::3] << 4) + (data[:, 2::3] & 0x0F)
            unpacked_data[:, 1::2] = (data[:, 1::3] << 4) + ((data[:, 2::3] >> 4) & 0x0F)
            data = unpacked_data
        elif s_bpp == 16:
            data = np.ascontiguousarray(data).view(np.uint16)
    
        return data

class PICAM2DNG(RPICAM2DNG):
    """For use within picamera2 library"""
    def options(self, compress=False) -> None:
        self.__tags_condition__(self.model.tags)
        self.tags = self.model.tags
        self.compress = compress
        self.path = ""
    


