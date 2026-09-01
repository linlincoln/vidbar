/* vidtx 定制发送端：基于 libcimbar（MPL-2.0）。
 *
 * 与上游 cimbar_send 的差异：
 *  - 单个 GLFW 播放窗口贯穿整个传输（多文件循环不重开窗口，位置不重置）；
 *  - 每轮每个文件只渲染 redundancy 倍的必需帧数（上游固定 8 倍，太浪费时间）；
 *  - 每个文件的 encode_id 在所有轮次中保持不变 —— 接收端未完成的喷泉流
 *    可以跨轮次持续汇聚（wirehair 种子确定，同一文件每轮产生相同的块）；
 *  - 可选 -o <video>：不开窗口，把帧写成 MJPEG 视频（模拟采集卡信道，
 *    用于无硬件回环测试；也是预渲染能力）。
 *  --rounds 0 表示无限循环，直到窗口被关闭或进程被终止。
 */

#include "cimb_translator/Config.h"
#include "encoder/EncoderPlus.h"
#include "gui/window_glfw.h"

#include "cxxopts/cxxopts.hpp"
#include "serialize/str.h"

#include <opencv2/videoio.hpp>

#include <GLFW/glfw3.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <iostream>
#include <string>
#include <thread>
#include <vector>

#ifdef _WIN32
#include <windows.h> // SetProcessDPIAware：禁用位图缩放，保证窗口尺寸/位置是物理像素
#endif

using std::string;
using std::vector;

namespace {

template <typename TP>
TP wait_for_frame_time(unsigned delay, const TP& start)
{
	unsigned millis = std::chrono::duration_cast<std::chrono::milliseconds>(
		std::chrono::high_resolution_clock::now() - start).count();
	if (delay > millis)
		std::this_thread::sleep_for(std::chrono::milliseconds(delay - millis));
	return std::chrono::high_resolution_clock::now();
}

unsigned parse_mode(const string& mode)
{
	if (mode == "4c" or mode == "4C")
		return 4;
	if (mode == "Bu" or mode == "BU")
		return 66;
	if (mode == "Bm" or mode == "BM")
		return 67;
	return 68; // B
}

// 解析 "WxH"（如 950x950）；失败返回 false
bool parse_size(const string& s, unsigned& w, unsigned& h)
{
	char x = 0;
	return std::sscanf(s.c_str(), "%ux%u%c", &w, &h, &x) == 2 and w > 0 and h > 0;
}

// 解析 "X,Y"（如 0,0）；失败返回 false
bool parse_pos(const string& s, int& x, int& y)
{
	char c = 0;
	return std::sscanf(s.c_str(), "%d,%d%c", &x, &y, &c) == 2;
}

} // namespace

int main(int argc, char** argv)
{
#ifdef _WIN32
	// 高 DPI（125%/150%）下若不声明 DPI 感知，Windows 会把窗口位图放大：
	// 双窗口并排时右窗溢出屏幕、左窗跨过接收端切缝，解码必挂。
	SetProcessDPIAware();
#endif

	cxxopts::Options options("vidbar_send", "vidtx sender: render cimbar frames to a window");

	unsigned compressionLevel = cimbar::Config::compression_level();
	options.add_options()
		("i,in", "Input files (chunk files; manifest first)", cxxopts::value<vector<string>>())
		("f,fps", "Target FPS", cxxopts::value<unsigned>()->default_value("30"))
		("r,rounds", "Number of full passes over all files. 0 = loop forever", cxxopts::value<unsigned>()->default_value("0"))
		("R,redundancy", "Frames per file, as multiple of required blocks", cxxopts::value<double>()->default_value("1.6"))
		("b,base", "Base encode_id (0-127). Must differ between transfers", cxxopts::value<unsigned>()->default_value("0"))
		("m,mode", "cimbar mode [B,Bm,Bu,4C]", cxxopts::value<string>()->default_value("B"))
		("z,compression", "zstd compression level. 0 == none", cxxopts::value<int>()->default_value(turbo::str::str(compressionLevel)))
		("p,padding", "Black padding around image in pixels", cxxopts::value<unsigned>()->default_value("32"))
		("win", "Window size WxH, e.g. 950x950 (default: fit code, clamped to screen to avoid taskbar overlap)", cxxopts::value<string>())
		("pos", "Window position X,Y, e.g. 0,0 (default: monitor top-left)", cxxopts::value<string>())
		("monitor", "Monitor index to place window on, 1-based (0 = primary)", cxxopts::value<int>()->default_value("0"))
		("list-monitors", "List available monitors and exit")
		("o,out", "Output MJPEG video file (headless mode: no window, for loopback testing)", cxxopts::value<string>())
		("h,help", "Print usage")
	;
	options.show_positional_help();
	options.parse_positional({"in"});
	options.positional_help("<in...>");

	auto result = options.parse(argc, argv);
	if (result.count("list-monitors"))
	{
		if (glfwInit() == GLFW_TRUE)
		{
			int count = 0;
			GLFWmonitor** mons = glfwGetMonitors(&count);
			for (int i = 0; i < count; ++i)
			{
				const GLFWvidmode* vm = glfwGetVideoMode(mons[i]);
				int mx = 0, my = 0;
				glfwGetMonitorPos(mons[i], &mx, &my);
				const char* name = glfwGetMonitorName(mons[i]);
				if (vm)
					std::cout << "monitor " << (i + 1) << ": " << vm->width << "x" << vm->height
						<< " +" << mx << "+" << my << " " << (name ? name : "") << std::endl;
			}
		}
		return 0;
	}

	if (result.count("help") or !result.count("in"))
	{
		std::cout << options.help() << std::endl;
		return 0;
	}

	vector<string> infiles = result["in"].as<vector<string>>();
	unsigned fps = result["fps"].as<unsigned>();
	unsigned rounds = result["rounds"].as<unsigned>();
	double redundancy = result["redundancy"].as<double>();
	unsigned baseId = result["base"].as<unsigned>() & 0x7F;
	compressionLevel = result["compression"].as<int>();
	unsigned padding = result["padding"].as<unsigned>();

	cimbar::Config::update(parse_mode(result["mode"].as<string>()));
	if (fps == 0)
		fps = 30;
	unsigned delay = 1000 / fps;

	const bool headless = result.count("out") != 0;

	// ---------- 无头模式：帧写入 MJPEG 视频（模拟采集卡信道） ----------
	if (headless)
	{
		string outfile = result["out"].as<string>();
		unsigned imgX = cimbar::Config::image_size_x();
		unsigned imgY = cimbar::Config::image_size_y();
		cv::Size frameSize(imgX + padding * 2, imgY + padding * 2);

		cv::VideoWriter vw;
		if (!vw.open(outfile, cv::VideoWriter::fourcc('M','J','P','G'), fps, frameSize, true))
		{
			std::cerr << "failed to open output video: " << outfile << std::endl;
			return 70;
		}

		EncoderPlus enc;
		unsigned round = 0;
		while (true)
		{
			++round;
			if (rounds != 0 and round > rounds)
				break;

			for (unsigned i = 0; i < infiles.size(); ++i)
			{
				const string& filename = infiles[i];
				enc.set_encode_id((baseId + i) & 0x7F);

				unsigned frames = enc.encode_fountain(filename, [&] (const cv::Mat& frame, unsigned) {
					cv::Mat canvas(frameSize, CV_8UC3, cv::Scalar(0, 0, 0));
					cv::Rect roi(padding, padding, frame.cols, frame.rows);
					frame.copyTo(canvas(roi));
					vw.write(canvas);
					return true;
				}, compressionLevel, redundancy);

				std::cerr << "round " << round << " file " << i << " (" << filename << "): "
					<< frames << " frames" << std::endl;
			}
		}
		vw.release();
		return 0;
	}

	// ---------- 窗口模式：GLFW 播放窗口 ----------
	// 注意：不启用 GLFW_SCALE_TO_MONITOR。高 DPI（125%/150%）下它会把窗口
	// 放大到超出屏幕、底部被任务栏遮挡——恰好盖住 cimbar 的四角锚点。

	if (glfwInit() != GLFW_TRUE)
	{
		std::cerr << "failed to init glfw :(" << std::endl;
		return 70;
	}

	// 目标显示器：--monitor N（1 起）；未指定时用主显示器
	int monitorIdx = result["monitor"].as<int>();
	GLFWmonitor* target = nullptr;
	{
		int count = 0;
		GLFWmonitor** mons = glfwGetMonitors(&count);
		if (monitorIdx >= 1 and monitorIdx <= count)
			target = mons[monitorIdx - 1];
		else if (count > 0)
			target = mons[0]; // GLFW 保证第一个是主显示器
	}
	int monX = 0, monY = 0;
	unsigned monW = 1920, monH = 1080;
	if (target)
	{
		if (const GLFWvidmode* vm = glfwGetVideoMode(target))
		{
			monW = vm->width;
			monH = vm->height;
			glfwGetMonitorPos(target, &monX, &monY);
		}
	}

	unsigned winX = cimbar::Config::image_size_x() + 16;
	unsigned winY = cimbar::Config::image_size_y() + 16;
	if (result.count("win"))
	{
		unsigned userW = 0, userH = 0;
		if (!parse_size(result["win"].as<string>(), userW, userH))
		{
			std::cerr << "bad --win format, expected WxH like 950x950" << std::endl;
			return 71;
		}
		winX = userW;
		winY = userH;
	}
	else
	{
		// 默认在目标显示器内自适应：预留任务栏高度，保证四角锚点完整可见
		// （glfwInit 幂等，window_glfw 构造时会再次调用，引用计数平衡）
		winX = std::min(winX, monW > 16 ? monW - 16 : 0);
		winY = std::min(winY, monH > 80 ? monH - 80 : 0);
	}

	// --pos 是相对目标显示器左上角的偏移；GLFW 需要虚拟桌面绝对坐标，
	// 必须加上显示器原点，否则选副屏时窗口会跑到主屏去。
	int posX = monX, posY = monY;  // 默认放在目标显示器左上角
	if (result.count("pos"))
	{
		int ux = 0, uy = 0;
		if (!parse_pos(result["pos"].as<string>(), ux, uy))
		{
			std::cerr << "bad --pos format, expected X,Y like 100,50" << std::endl;
			return 71;
		}
		posX = monX + ux;
		posY = monY + uy;
	}

	cimbar::window_glfw window(winX, winY, "vidtx player - drag onto captured screen");
	if (!window.is_good())
	{
		std::cerr << "failed to create window :(" << std::endl;
		return 70;
	}
	window.auto_scale_to_window(padding);
	window.set_pos(posX, posY);

	EncoderPlus enc;
	unsigned round = 0;
	while (true)
	{
		++round;
		if (rounds != 0 and round > rounds)
			break;

		for (unsigned i = 0; i < infiles.size(); ++i)
		{
			if (window.should_close())
				return 0;

			const string& filename = infiles[i];

			// encode_id 在轮次间保持不变：接收端可跨轮续传
			enc.set_encode_id((baseId + i) & 0x7F);

			std::chrono::time_point start = std::chrono::high_resolution_clock::now();
			unsigned frames = enc.encode_fountain(filename, [&] (const cv::Mat& frame, unsigned) {
				start = wait_for_frame_time(delay, start);
				window.show(frame, 0);
				window.shake();
				return !window.should_close();
			}, compressionLevel, redundancy);

			std::cerr << "round " << round << " file " << i << " (" << filename << "): "
				<< frames << " frames" << std::endl;
			if (window.should_close())
				return 0;
		}
	}

	return 0;
}
