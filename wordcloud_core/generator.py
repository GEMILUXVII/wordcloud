"""
词云生成核心模块
"""

import os
import time
import datetime
import shutil
import traceback
import threading
from typing import Dict, List, Optional, Tuple
from collections import Counter
from pathlib import Path

import numpy as np
from wordcloud import WordCloud
import matplotlib.pyplot as plt
import matplotlib

matplotlib.use("Agg")  # 使用非交互式后端
from matplotlib.font_manager import FontProperties
from PIL import Image, ImageDraw, ImageFont, ImageStat
from astrbot.api import logger
from astrbot.api.star import StarTools

from ..utils import segment_text, load_stop_words, get_image_path
from ..constant import (
    DEFAULT_WIDTH,
    DEFAULT_HEIGHT,
    DEFAULT_MAX_WORDS,
    DEFAULT_BACKGROUND_COLOR,
    DEFAULT_COLORMAP,
    DEFAULT_MIN_WORD_LENGTH,
    PLUGIN_DIR,
    DATA_DIR,
    PLUGIN_NAME,
)

# 全局锁，用于防止多个线程同时生成相同的词云
_WORDCLOUD_LOCKS = {}
_GLOBAL_LOCK = threading.Lock()


# 确保当前词云生成请求唯一性的方法
def _get_lock_for_key(key: str) -> threading.Lock:
    """
    获取指定键的锁对象，如果不存在则创建
    """
    with _GLOBAL_LOCK:
        if key not in _WORDCLOUD_LOCKS:
            _WORDCLOUD_LOCKS[key] = threading.Lock()
        return _WORDCLOUD_LOCKS[key]


class WordCloudGenerator:
    """词云生成器类"""

    def __init__(
        self,
        width: int = DEFAULT_WIDTH,
        height: int = DEFAULT_HEIGHT,
        max_words: int = DEFAULT_MAX_WORDS,
        background_color: str = DEFAULT_BACKGROUND_COLOR,
        colormap: str = DEFAULT_COLORMAP,
        font_path: Optional[str] = None,
        min_word_length: int = DEFAULT_MIN_WORD_LENGTH,
        stop_words_file: Optional[str] = None,
        shape: str = "rectangle",  # 修改默认形状为矩形
        custom_mask_path: Optional[str] = None,  # 添加自定义蒙版路径参数
        min_font_size: int = 8,  # 添加最小字体大小参数
        max_font_size: int = 200,  # 添加最大字体大小参数
        min_word_frequency: int = 1,  # 新增：最小词频参数
    ):
        """
        初始化词云生成器

        Args:
            width: 词云图片宽度
            height: 词云图片高度
            max_words: 最大词数量
            background_color: 背景颜色
            colormap: 颜色映射
            font_path: 字体路径
            min_word_length: 最小词长度
            stop_words_file: 停用词文件路径
            shape: 词云形状，支持"circle"和"rectangle"
            custom_mask_path: 自定义蒙版图片路径
            min_font_size: 最小字体大小，用于低频词
            max_font_size: 最大字体大小，用于高频词
        """
        self.width = width
        self.height = height
        self.max_words = max_words
        self.background_color = background_color
        self.colormap = colormap
        self.shape = shape
        self.custom_mask_path = custom_mask_path  # 保存自定义蒙版路径
        self.min_font_size = min_font_size  # 保存最小字体大小
        self.max_font_size = max_font_size  # 保存最大字体大小
        self.min_word_frequency = min_word_frequency  # 新增：保存最小词频

        # 获取数据目录，优先使用StarTools确保可用
        data_dir = None
        try:
            # 先尝试通过StarTools获取数据目录，这是最可靠的方式
            data_dir = StarTools.get_data_dir(PLUGIN_NAME)
            logger.info(f"通过StarTools获取数据目录: {data_dir}")
        except Exception as e:
            logger.warning(f"通过StarTools获取数据目录失败: {e}")
            # 尝试使用全局DATA_DIR
            if DATA_DIR is not None:
                data_dir = DATA_DIR
                logger.info(f"使用全局定义的DATA_DIR: {data_dir}")
            else:
                # 无法获取数据目录，使用临时目录作为备用
                temp_data_dir = PLUGIN_DIR / "temp_data"
                temp_data_dir.mkdir(exist_ok=True)
                data_dir = temp_data_dir
                logger.warning(f"无法获取标准数据目录，使用临时目录: {temp_data_dir}")

        # 确保资源目录存在
        resources_dir = data_dir / "resources"
        resources_dir.mkdir(exist_ok=True)
        fonts_dir = resources_dir / "fonts"
        fonts_dir.mkdir(exist_ok=True)

        # 设置默认字体路径，从插件目录复制到数据目录
        plugin_font_path = PLUGIN_DIR / "fonts" / "LXGWWenKai-Regular.ttf"
        data_font_path = fonts_dir / "LXGWWenKai-Regular.ttf"

        # 如果数据目录中没有字体，尝试从插件目录复制
        if not data_font_path.exists() and plugin_font_path.exists():
            try:
                shutil.copy(plugin_font_path, data_font_path)
                logger.info(f"已将字体文件复制到数据目录: {data_font_path}")
            except Exception as e:
                logger.warning(f"复制字体文件失败: {e}")

        # 处理字体路径
        if font_path and os.path.exists(font_path):
            # 如果是相对路径，可能需要相对于插件目录解析
            if not os.path.isabs(font_path):
                abs_font_path = PLUGIN_DIR / font_path
                if os.path.exists(abs_font_path):
                    self.font_path = str(abs_font_path)
                    logger.info(f"使用插件目录中的字体: {self.font_path}")
                else:
                    # 尝试相对于数据目录
                    data_relative_font_path = (
                        data_dir / "resources" / "fonts" / os.path.basename(font_path)
                    )
                    if os.path.exists(data_relative_font_path):
                        self.font_path = str(data_relative_font_path)
                        logger.info(f"使用数据目录中的字体: {self.font_path}")
                    else:
                        self.font_path = (
                            font_path  # 使用原始路径，可能是相对于当前工作目录
                        )
            else:
                self.font_path = font_path  # 使用绝对路径
        elif data_font_path.exists():
            self.font_path = str(data_font_path)
            logger.info(f"使用数据目录中的字体: {self.font_path}")
        elif plugin_font_path.exists():
            self.font_path = str(plugin_font_path)
            logger.info(f"使用插件目录中的字体: {self.font_path}")
        else:
            self.font_path = None
            logger.warning("未找到有效字体文件，将使用系统默认字体")

        # 处理停用词文件
        if stop_words_file:
            # 处理相对路径
            if not os.path.isabs(stop_words_file):
                # 尝试相对于插件目录解析
                plugin_stopwords_path = PLUGIN_DIR / stop_words_file
                data_stopwords_path = (
                    data_dir / "resources" / os.path.basename(stop_words_file)
                )

                # 如果插件目录有文件但数据目录没有，复制过去
                if plugin_stopwords_path.exists() and not data_stopwords_path.exists():
                    try:
                        shutil.copy(plugin_stopwords_path, data_stopwords_path)
                        logger.info(
                            f"已将停用词文件复制到数据目录: {data_stopwords_path}"
                        )
                        # 使用数据目录中的文件
                        stop_words_file = str(data_stopwords_path)
                    except Exception as e:
                        logger.warning(f"复制停用词文件失败: {e}")
                        # 如果复制失败，使用插件目录中的文件
                        if plugin_stopwords_path.exists():
                            stop_words_file = str(plugin_stopwords_path)
                elif data_stopwords_path.exists():
                    # 使用数据目录中的文件
                    stop_words_file = str(data_stopwords_path)
                elif plugin_stopwords_path.exists():
                    # 使用插件目录中的文件
                    stop_words_file = str(plugin_stopwords_path)

        self.min_word_length = min_word_length
        self.stop_words = load_stop_words(stop_words_file)

        # 保存临时使用的data_dir
        self._temp_data_dir = data_dir

        # 初始化词云生成器
        self._init_wordcloud()

    def _create_circle_mask(self):
        """
        创建圆形蒙版

        在WordCloud中，蒙版的工作方式与直觉相反：
        - 值为0的区域允许绘制文字
        - 值为非0（如255）的区域不允许绘制文字

        为生成在圆形内部的词云，我们需要：
        1. 创建一个全为255的数组（默认不允许绘制）
        2. 将圆形内部区域设置为0（允许绘制）
        3. 确保圆形外部区域保持为255（不允许绘制）
        """
        # 创建一个正方形画布，边长取width和height的最大值确保圆形不会被压缩
        size = max(self.width, self.height)

        # 创建一个全255数组作为基础蒙版（默认不允许绘制）
        mask = np.ones((size, size), dtype=np.uint8) * 255

        # 计算圆心和半径
        center = size // 2
        radius = int(center * 0.9)  # 使用较小的半径避免太靠近边缘

        # 创建一个网格坐标系用于计算每个点到圆心的距离
        y, x = np.ogrid[:size, :size]

        # 计算每个点到圆心的距离的平方
        dist_from_center = (x - center) ** 2 + (y - center) ** 2

        # 圆内区域的布尔掩码（True表示在圆内）
        circle = dist_from_center <= radius**2

        # 将圆内区域设为0（允许绘制文字），其余区域保持为255（不绘制文字）
        mask[circle] = 0

        # 验证蒙版：记录圆内（值为0）像素的数量和总像素数
        circle_pixels = np.sum(mask == 0)
        total_pixels = size * size
        circle_ratio = circle_pixels / total_pixels

        logger.info(f"生成圆形蒙版: 大小={size}x{size}, 半径={radius}")
        logger.info(
            f"圆内像素数量: {circle_pixels}, 总像素数: {total_pixels}, 比例: {circle_ratio:.2f}"
        )

        return mask

    def _create_diamond_mask(self, width: int, height: int):
        """创建菱形蒙版 (白色背景，黑色形状 - 词云绘制区域)
        词云库通常期望蒙版中值为0的区域绘制文字，非0区域不绘制。
        所以我们画黑色菱形在白色背景上，然后转换时黑色变0，白色变255.
        """
        img = Image.new("L", (width, height), 255)  # 白色背景 (不绘制区域)
        draw = ImageDraw.Draw(img)
        # 定义菱形的四个顶点
        # (width/2, 0), (width, height/2), (width/2, height), (0, height/2)
        points = [
            (width // 2, 0),
            (width, height // 2),
            (width // 2, height - 1),  # height-1 to avoid going out of bounds
            (0, height // 2),
        ]
        draw.polygon(points, fill=0)  # 黑色菱形 (绘制区域)
        mask = np.array(img)
        logger.info(f"生成菱形蒙版: 大小={width}x{height}")
        return mask

    def _create_triangle_mask(self, width: int, height: int):
        """创建上三角形蒙版 (白色背景，黑色形状 - 词云绘制区域)"""
        img = Image.new("L", (width, height), 255)  # 白色背景
        draw = ImageDraw.Draw(img)
        # 定义上三角形的三个顶点
        # (width/2, 0), (width, height), (0, height)
        points = [
            (width // 2, 0),
            (width - 1, height - 1),  # width-1, height-1 to avoid going out of bounds
            (0, height - 1),
        ]
        draw.polygon(points, fill=0)  # 黑色三角形
        mask = np.array(img)
        logger.info(f"生成上三角形蒙版: 大小={width}x{height}")
        return mask

    def _create_cloud_mask(self, width: int, height: int):
        """创建底部平坦、顶部具有3-4个起伏圆弧的云朵形状蒙版"""
        img = Image.new("L", (width, height), 255)  # 白色背景 (不绘制区域)
        draw = ImageDraw.Draw(img)

        y_bottom_line_factor = 0.7  # 平底从高度的70%处开始

        # 定义构成顶部起伏的椭圆参数 (cx, cy, rx, ry)
        # cx: 中心点X轴比例, cy: 中心点Y轴比例
        # rx: X轴半径比例, ry: Y轴半径比例

        ellipses_params = [
            # 尝试构成3个主要、较宽的顶部凸起，以及一个更小的顶部点缀
            # 主要凸起1 (中间，最高)
            (0.50, 0.35, 0.25, 0.22),  # Y中心0.35, Y半径0.22 -> 顶部在0.13, 底部在0.57
            # 主要凸起2 (左侧)
            (
                0.25,
                0.45,
                0.28,
                0.20,
            ),  # Y中心0.45, Y半径0.20 -> 顶部在0.25, 底部在0.65. X左边缘0.25-0.28 = -0.03 (会裁剪到0)
            # 主要凸起3 (右侧)
            (
                0.75,
                0.45,
                0.28,
                0.20,
            ),  # Y中心0.45, Y半径0.20 -> 顶部在0.25, 底部在0.65. X右边缘0.75+0.28 = 1.03 (会裁剪到1)
            # 额外的顶部小凸起，增加起伏感 (可选，如果上面3个效果够好，可以移除或调整)
            (0.50, 0.20, 0.12, 0.10),  # 更小的，在中央凸起之上
        ]

        min_x_coord = width
        max_x_coord = 0

        for cx_f, cy_f, rx_f, ry_f in ellipses_params:
            center_x = int(width * cx_f)
            center_y = int(height * cy_f)
            radius_x = max(1, int(width * rx_f))
            radius_y = max(1, int(height * ry_f))

            bbox = (
                center_x - radius_x,
                center_y - radius_y,
                center_x + radius_x,
                center_y + radius_y,
            )
            draw.ellipse(bbox, fill=0)  # 值为0的区域是词云绘制区

            min_x_coord = min(min_x_coord, center_x - radius_x)
            max_x_coord = max(max_x_coord, center_x + radius_x)

        # 确保 min_x 和 max_x 在图像范围内
        min_x_coord = max(0, min_x_coord)
        max_x_coord = min(width - 1, max_x_coord)

        # 绘制平坦的底部矩形
        if min_x_coord < max_x_coord:  # 只有当云朵有宽度时才画底部
            flat_bottom_y_start = int(height * y_bottom_line_factor)

            fill_rect_bbox = (
                min_x_coord,
                flat_bottom_y_start,
                max_x_coord,
                height - 1,  # 延伸到图像底部
            )
            draw.rectangle(fill_rect_bbox, fill=0)

        mask = np.array(img)
        logger.info(
            f"通过程序化绘制生成顶部起伏、底部平坦的云朵蒙版: 大小={width}x{height}"
        )
        return mask

    def _init_wordcloud(self) -> None:
        """初始化词云生成器"""
        # 如果形状设置为圆形，创建圆形蒙版
        mask = None
        processed_custom_mask = False  # 标记是否成功处理了自定义蒙版

        # 优先处理自定义蒙版
        if self.custom_mask_path:
            mask_image_path = None
            # 检查是绝对路径还是相对路径
            if os.path.isabs(self.custom_mask_path):
                mask_image_path = Path(self.custom_mask_path)
            else:
                # 相对路径，相对于插件数据目录下的 resources/images/
                if self._temp_data_dir:  # _temp_data_dir 在 __init__ 中设置
                    mask_image_path = (
                        self._temp_data_dir
                        / "resources"
                        / "images"
                        / self.custom_mask_path
                    )
                else:  # Fallback if _temp_data_dir is somehow not set
                    mask_image_path = (
                        PLUGIN_DIR / "resources" / "images" / self.custom_mask_path
                    )

            if (
                mask_image_path
                and mask_image_path.exists()
                and mask_image_path.is_file()
            ):
                try:
                    logger.info(f"加载自定义蒙版图片: {mask_image_path}")
                    custom_mask_image = Image.open(mask_image_path)
                    mask = np.array(custom_mask_image)
                    # 确保蒙版是2D的 (灰度图或alpha通道)
                    if mask.ndim == 3:
                        # 如果是RGB(A)，尝试取一个通道，比如红色，或者转换为灰度
                        # WordCloud 通常期望蒙版是单通道的，非零表示区域，零表示空白
                        # 但更常见的做法是白色(255)为忽略区域，黑色(0)或深色为绘制区域
                        # 如果是RGBA，第四个通道是alpha，也可以用。这里我们简单转灰度
                        # Image.open().convert('L') 之后再 np.array() 是更标准做法
                        # 为了安全，重新用 convert('L') 加载
                        custom_mask_image_gray = Image.open(mask_image_path).convert(
                            "L"
                        )
                        mask = np.array(custom_mask_image_gray)
                        logger.info("自定义蒙版已转换为灰度图.")

                    # 检查蒙版的值范围，wordcloud期望非绘制区域为255
                    # 如果蒙版主要是深色背景，浅色图案，可能需要反转
                    # 例如，如果用户提供的是黑底白云的图片，需要转换
                    # 这里我们假设用户提供的图片是白底黑图案 (黑色区域为词云形状)
                    # wordcloud库会将蒙版中值为0或接近0的区域视为绘制区域，255为忽略区域
                    # 所以，如果我们的图片是黑形状白背景，Pillow读入后黑是0，白是255，正好符合预期
                    logger.info(
                        f"自定义蒙版加载成功，形状: {mask.shape}, 类型: {mask.dtype}"
                    )
                    processed_custom_mask = True
                except Exception as e:
                    logger.error(
                        f"加载或处理自定义蒙版图片失败: {mask_image_path}, 错误: {e}"
                    )
                    mask = None  # 加载失败，不使用蒙版
            else:
                logger.warning(
                    f"自定义蒙版图片路径无效或文件不存在: {self.custom_mask_path} (解析后路径: {mask_image_path})"
                )

        # 如果没有成功处理自定义蒙版，再根据 shape 参数创建预设蒙版
        if not processed_custom_mask:
            if self.shape == "circle":
                mask = self._create_circle_mask()
            elif self.shape == "diamond":
                mask = self._create_diamond_mask(self.width, self.height)
            elif self.shape == "triangle_up":
                mask = self._create_triangle_mask(self.width, self.height)
            elif self.shape == "cloud":
                mask = self._create_cloud_mask(self.width, self.height)
            # 对于 "rectangle" 或其他未指定蒙版的形状，mask 保持为 None，词云将默认为矩形        # 词云参数
        wordcloud_params = {
            "width": self.width,
            "height": self.height,
            "max_words": self.max_words,
            "background_color": self.background_color,
            "colormap": self.colormap,
            "min_font_size": self.min_font_size,
            "max_font_size": self.max_font_size,
            "random_state": 42,
            "collocations": False,  # 避免重复显示词组
            "normalize_plurals": False,
            "mask": mask,  # 设置蒙版
            "prefer_horizontal": 0.9,  # 调整为90%水平显示，增加布局多样性
            "repeat": False,  # 不重复使用词以填满空间，避免文字出现在不应该出现的地方
            "mode": "RGB",  # 使用RGB模式，避免与轮廓绘制时的通道不匹配问题
        }

        # 添加轮廓效果，增强形状
        if self.shape == "circle":
            # 由于通道不匹配问题，暂时禁用轮廓效果
            # wordcloud_params['contour_width'] = 1
            # wordcloud_params['contour_color'] = self.background_color
            pass

        # 如果提供了字体路径，则使用它
        if self.font_path and os.path.exists(self.font_path):
            wordcloud_params["font_path"] = self.font_path

        self.wordcloud = WordCloud(**wordcloud_params)

    def process_text(self, text: str) -> List[str]:
        """
        处理文本，进行分词和过滤

        Args:
            text: 输入文本

        Returns:
            处理后的词语列表
        """
        return segment_text(text, self.min_word_length, self.stop_words)

    def process_texts(self, texts: List[str]) -> Dict[str, int]:
        """
        处理多条文本，统计词频

        Args:
            texts: 文本列表

        Returns:
            词频统计字典
        """
        # 合并所有文本并分词
        all_words = []
        for text in texts:
            words = self.process_text(text)
            all_words.extend(words)

        # 统计词频
        word_counts = Counter(all_words)
        return dict(word_counts)

    def _filter_by_frequency(self, word_counts: Dict[str, int]) -> Dict[str, int]:
        """
        根据最小词频过滤词汇。

        Args:
            word_counts: 原始词频统计。

        Returns:
            过滤后的词频统计。
        """
        if self.min_word_frequency <= 1:
            return word_counts  # 如果最小词频设置为1或更小，则不进行过滤

        filtered_counts = {
            word: count
            for word, count in word_counts.items()
            if count >= self.min_word_frequency
        }
        logger.info(
            f"词频过滤 (min_freq={self.min_word_frequency}): 原始词汇 {len(word_counts)}个 -> 过滤后 {len(filtered_counts)}个"
        )
        return filtered_counts

    def _add_timestamp_to_image(
        self, img: Image.Image, timestamp: Optional[int] = None
    ) -> Image.Image:
        """
        向图片添加时间戳水印

        Args:
            img: 原始图片
            timestamp: 时间戳，默认为当前时间

        Returns:
            添加水印后的图片
        """
        if timestamp is None:
            timestamp = int(time.time())

        # 格式化时间戳
        time_str = f"生成时间: {datetime.datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')}"

        # 创建可绘制对象
        draw = ImageDraw.Draw(img, "RGBA")

        # 字体大小
        font_size = 14

        try:
            # 尝试加载自定义字体
            font = None
            try:
                if self.font_path and os.path.exists(self.font_path):
                    # 尝试加载指定的字体
                    try:
                        font = ImageFont.truetype(self.font_path, font_size)
                    except:
                        # 如果加载失败，尝试使用默认字体
                        font = ImageFont.load_default()
                        logger.warning(f"加载指定字体失败: {self.font_path}")
                else:
                    # 尝试从系统中查找可用的中文字体
                    system_fonts = [
                        # Windows中文字体
                        "C:/Windows/Fonts/simhei.ttf",  # 黑体
                        "C:/Windows/Fonts/simsun.ttc",  # 宋体
                        "C:/Windows/Fonts/simkai.ttf",  # 楷体
                        "C:/Windows/Fonts/msyh.ttc",  # 微软雅黑
                        # Linux中文字体
                        "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
                        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
                        # macOS中文字体
                        "/System/Library/Fonts/PingFang.ttc",
                    ]

                    for font_path in system_fonts:
                        try:
                            if os.path.exists(font_path):
                                font = ImageFont.truetype(font_path, font_size)
                                logger.debug(f"成功加载系统字体: {font_path}")
                                break
                        except:
                            continue
            except:
                font = ImageFont.load_default()
        except Exception as e:
            logger.warning(f"加载字体失败: {e}，将使用默认字体")
            font = ImageFont.load_default()

        # 添加水印位置偏移量，确保文字放置位置合适
        margin = 10

        # 获取文本大小，用于定位
        try:
            if hasattr(font, "getbbox"):
                text_width, text_height = font.getbbox(time_str)[2:]
            else:
                text_width, text_height = font.getsize(time_str)
        except:
            # 如果无法获取文本大小，使用估计值
            text_width, text_height = len(time_str) * font_size // 2, font_size

        # 计算文字位置 - 左下角
        position = (margin, img.height - text_height - margin)

        # 检查背景颜色并确定文字颜色
        try:
            # 获取左下角区域的主要颜色
            sample_box = (0, img.height - text_height * 2, text_width * 2, img.height)
            sample_img = img.crop(sample_box)

            # 检查图像模式，如果有Alpha通道，转换为RGB
            if sample_img.mode == "RGBA":
                # 创建白色背景
                bg = Image.new("RGB", sample_img.size, (255, 255, 255))
                # 复合Alpha通道
                sample_img = Image.alpha_composite(
                    bg.convert("RGBA"), sample_img
                ).convert("RGB")

            # 计算平均亮度
            avg_rgb = ImageStat.Stat(sample_img).mean
            brightness = sum(avg_rgb) / len(avg_rgb)
            is_dark_bg = brightness < 128

            # 根据背景选择文字颜色
            text_color = (255, 255, 255) if is_dark_bg else (0, 0, 0)
            bg_color = (0, 0, 0, 180) if is_dark_bg else (255, 255, 255, 180)

            logger.debug(
                f"检测到{'深色' if is_dark_bg else '浅色'}背景，亮度值: {brightness:.1f}"
            )

        except Exception as e:
            # 出错时使用默认设置
            logger.warning(f"检测背景颜色失败: {e}，使用默认颜色")
            # 默认假设是深色背景
            text_color = (255, 255, 255)  # 白色文字
            bg_color = (0, 0, 0, 180)  # 半透明黑色背景

        # 使用半透明背景增加可读性
        bg_padding = 4
        bg_box = [
            position[0] - bg_padding,
            position[1] - bg_padding,
            position[0] + text_width + bg_padding,
            position[1] + text_height + bg_padding,
        ]

        # 绘制半透明背景
        draw.rectangle(bg_box, fill=bg_color)

        # 绘制文字
        draw.text(position, time_str, fill=text_color, font=font)

        return img

    def generate_wordcloud(
        self,
        word_counts: Dict[str, int],
        session_id: str,
        timestamp: Optional[int] = None,
        title: Optional[str] = None,
    ) -> Tuple[str, Path]:
        """
        生成词云图片

        Args:
            word_counts: 词频统计
            session_id: 会话ID
            timestamp: 时间戳，为None则使用当前时间
            title: 词云标题

        Returns:
            生成的图片路径(字符串), 路径对象
        """
        if timestamp is None:
            timestamp = int(time.time())

        if not word_counts:
            raise ValueError("无有效词频数据，无法生成词云")

        # 在生成词云前，根据配置的最小词频过滤词汇
        filtered_word_counts = self._filter_by_frequency(word_counts)

        if not filtered_word_counts:
            # 如果过滤后没有词了，可以抛出错误或者生成一个提示性的空图片
            # 这里我们选择抛出错误，因为通常这意味着数据不足或过滤条件太严格
            logger.warning("根据最小词频过滤后，没有足够的词汇来生成词云。")
            raise ValueError("过滤后无有效词频数据，无法生成词云")

        # 获取图片存储路径
        image_path = get_image_path(session_id, timestamp)

        # 创建锁的键名
        lock_key = f"wordcloud_{session_id}_{timestamp}"

        # 获取锁对象
        lock = _get_lock_for_key(lock_key)

        # 尝试获取锁
        if not lock.acquire(blocking=False):
            logger.warning(
                f"已有其他线程正在生成相同的词云 {session_id}_{timestamp}，跳过本次生成"
            )

            # 如果文件已存在，直接返回路径
            if image_path.exists():
                logger.info(f"使用已存在的词云图片: {image_path}")
                return str(image_path), image_path

            # 等待一段时间看是否生成了
            try:
                wait_start = time.time()
                while time.time() - wait_start < 5.0:  # 最多等待5秒
                    time.sleep(0.5)
                    if image_path.exists():
                        logger.info(f"等待后找到了词云图片: {image_path}")
                        return str(image_path), image_path

                # 如果等待超时仍未生成，则抛出异常
                raise ValueError("等待词云生成超时，请稍后再试")
            except Exception as e:
                logger.error(f"等待词云生成时出错: {e}")
                raise ValueError("词云生成被其他任务占用，请稍后再试")

        try:
            # 生成词云
            self.wordcloud.generate_from_frequencies(
                filtered_word_counts
            )  # 使用过滤后的词频

            # 确保目录存在
            image_path.parent.mkdir(parents=True, exist_ok=True)

            # 先保存词云图像到临时文件，避免直接操作wordcloud对象导致维度不匹配
            temp_path = image_path.parent / f"temp_{image_path.name}"
            self.wordcloud.to_file(str(temp_path))

            # 读取保存的图像
            wordcloud_img = np.array(Image.open(temp_path))

            # 使用matplotlib创建带标题的完整图像
            fig_width, fig_height = 10, 6.5
            dpi = 150

            # 创建带有背景色的图表
            fig = plt.figure(
                figsize=(fig_width, fig_height),
                facecolor=self.background_color,
                dpi=dpi,
            )
            plt.rcParams.update({"figure.autolayout": True})
            ax = plt.axes()
            ax.set_facecolor(self.background_color)
            ax.set_position([0, 0, 1, 0.9])  # 为标题留出少量空间

            # 去除边框和刻度
            plt.axis("off")
            plt.box(False)
            plt.tight_layout(pad=0.1)  # 减少内边距

            # 绘制词云图像
            plt.imshow(wordcloud_img, interpolation="bilinear")

            # 设置标题，使用对比色
            if title:
                # 选择与背景相反的颜色
                title_color = (
                    "white" if self._is_dark_color(self.background_color) else "black"
                )

                logger.info(
                    f"设置词云标题: {title}, 背景色: {self.background_color}, 标题颜色: {title_color}"
                )

                # 设置中文标题字体
                if self.font_path and os.path.exists(self.font_path):
                    try:
                        font_prop = FontProperties(fname=self.font_path)
                        plt.title(
                            title,
                            fontproperties=font_prop,
                            fontsize=16,
                            pad=10,
                            color=title_color,
                        )
                    except Exception as e:
                        logger.warning(f"使用自定义字体设置标题失败: {e}")
                        plt.title(title, fontsize=16, pad=10, color=title_color)
                else:
                    plt.title(title, fontsize=16, pad=10, color=title_color)

                # 如果是深色背景，添加文字边框增强可读性
                if self._is_dark_color(self.background_color):
                    try:
                        # 将当前标题获取出来
                        title_obj = ax.get_title()
                        # 清除原标题
                        ax.set_title("")
                        # 重新设置带边框的标题
                        plt.title(
                            title,
                            fontproperties=font_prop
                            if "font_prop" in locals()
                            else None,
                            fontsize=16,
                            pad=10,
                            color=title_color,
                            bbox=dict(
                                facecolor=self.background_color,
                                alpha=0.8,
                                edgecolor="white",
                                boxstyle="round,pad=0.5",
                            ),
                        )
                    except Exception as title_ex:
                        logger.warning(f"设置标题边框失败: {title_ex}")
                        # 恢复原标题
                        if "title_obj" in locals():
                            ax.set_title(title_obj)

            # 保存图片
            plt.savefig(
                image_path,
                bbox_inches="tight",
                pad_inches=0.2,  # 减少边距
                dpi=dpi,
                facecolor=self.background_color,
            )
            plt.close(fig)

            # 删除临时文件
            try:
                if temp_path.exists():
                    os.remove(temp_path)
            except Exception as e:
                logger.warning(f"删除临时文件失败: {e}")

            # 添加时间戳水印
            img = Image.open(image_path)
            final_image = self._add_timestamp_to_image(img, timestamp)
            final_image.save(image_path)

            # 输出图片信息
            logger.info(f"词云图片已保存至: {image_path}")

            return str(image_path), image_path
        except Exception as e:
            logger.error(f"生成词云时出错: {e}")
            logger.error(traceback.format_exc())
            raise
        finally:
            # 释放锁
            lock.release()

    def _is_dark_color(self, color_str: str) -> bool:
        """
        判断颜色是否为深色

        Args:
            color_str: 颜色字符串，可以是颜色名称或十六进制值

        Returns:
            是否为深色
        """
        # 处理常见颜色名称
        dark_color_names = [
            "black",
            "darkblue",
            "darkgreen",
            "darkcyan",
            "darkred",
            "darkmagenta",
            "darkgray",
            "darkgrey",
            "navy",
            "green",
            "teal",
            "maroon",
            "purple",
            "indigo",
            "midnightblue",
            "darkslategray",
            "darkslategrey",
            "dimgray",
            "dimgrey",
        ]

        light_color_names = [
            "white",
            "lightgray",
            "lightgrey",
            "whitesmoke",
            "snow",
            "ivory",
            "floralwhite",
            "linen",
            "cornsilk",
            "seashell",
            "lavenderblush",
            "papayawhip",
            "blanchedalmond",
        ]

        # 首先检查确定的颜色名称
        color_lower = color_str.lower()
        if color_lower in dark_color_names:
            logger.debug(f"颜色 {color_str} 在已知深色列表中")
            return True
        if color_lower in light_color_names:
            logger.debug(f"颜色 {color_str} 在已知浅色列表中")
            return False

        # 处理十六进制颜色值
        if color_str.startswith("#"):
            try:
                # 去掉#号并解析RGB值
                r, g, b = (
                    int(color_str[1:3], 16),
                    int(color_str[3:5], 16),
                    int(color_str[5:7], 16),
                )
                # 计算亮度 (使用更精确的亮度计算公式)
                # 这个公式来自W3C标准：https://www.w3.org/TR/WCAG20-TECHS/G17.html
                brightness = (r * 299 + g * 587 + b * 114) / 1000
                is_dark = brightness < 128
                logger.debug(
                    f"颜色 {color_str} 亮度值: {brightness:.1f}, 判定为{'深色' if is_dark else '浅色'}"
                )
                return is_dark
            except Exception as e:
                logger.warning(f"解析十六进制颜色失败: {color_str}, {e}")
                return False  # 解析失败，默认为浅色

        # 尝试使用matplotlib的颜色名称
        try:
            from matplotlib.colors import to_rgb

            rgb = to_rgb(color_str)
            r, g, b = int(rgb[0] * 255), int(rgb[1] * 255), int(rgb[2] * 255)
            brightness = (r * 299 + g * 587 + b * 114) / 1000
            is_dark = brightness < 128
            logger.debug(
                f"颜色名称 {color_str} 转换为RGB: {r},{g},{b}, 亮度值: {brightness:.1f}, 判定为{'深色' if is_dark else '浅色'}"
            )
            return is_dark
        except Exception as e:
            logger.warning(f"解析颜色名称失败: {color_str}, {e}")
            return False  # 解析失败，默认为浅色

    def _filter_word_frequencies(self, word_counts: Dict[str, int]) -> Dict[str, int]:
        """
        过滤词频，移除频率过低的词汇，让词云更加利落

        Args:
            word_counts: 原始词频统计

        Returns:
            过滤后的词频统计
        """
        if not word_counts:
            return word_counts

        # 计算词频统计信息
        frequencies = list(word_counts.values())
        total_words = len(frequencies)
        max_freq = max(frequencies)

        # 动态计算最小频率阈值
        # 如果词汇总数很多，设置更严格的过滤条件
        if total_words > self.max_words * 3:
            # 词汇过多时，使用更严格的过滤
            min_freq_threshold = max(2, max_freq * 0.02)  # 至少2次，或最高频的2%
        elif total_words > self.max_words * 2:
            # 词汇较多时，适中过滤
            min_freq_threshold = max(1, max_freq * 0.01)  # 至少1次，或最高频的1%
        else:
            # 词汇不多时，轻度过滤
            min_freq_threshold = 1

        # 过滤低频词
        filtered_counts = {
            word: count
            for word, count in word_counts.items()
            if count >= min_freq_threshold
        }

        # 如果过滤后词汇仍然过多，取频率最高的词汇
        if len(filtered_counts) > self.max_words:
            # 按频率排序，取前max_words个
            sorted_words = sorted(
                filtered_counts.items(), key=lambda x: x[1], reverse=True
            )
            filtered_counts = dict(sorted_words[: self.max_words])

        logger.info(
            f"词频过滤: 原始词汇{total_words}个 -> 过滤后{len(filtered_counts)}个，最小频率阈值: {min_freq_threshold}"
        )

        return filtered_counts
