/* vidtx 定制接收端：基于 libcimbar（MPL-2.0）。
 *
 * 与上游 cimbar_recv2 的差异：
 *  - 按设备索引 + 指定后端(DSHOW/V4L2)打开采集卡，可设置 MJPG、分辨率、帧率；
 *    也支持把 -i 传视频文件路径（用于无硬件回环测试）。
 *  - 纯 CLI，无预览窗口依赖。
 *  - stdout 输出单行 JSON 事件（open/stat/saved/error），供 server.py 解析。
 *  - 周期输出统计：实际采集帧率、解码成功数、各喷泉流进度。
 */

#include "cimb_translator/Config.h"
#include "compression/zstd_decompressor.h"
#include "compression/zstd_header_check.h"
#include "encoder/Decoder.h"
#include "extractor/Extractor.h"
#include "fountain/fountain_decoder_sink.h"
#include "serialize/format.h"
#include "util/File.h"

#include "cxxopts/cxxopts.hpp"
#include "serialize/str.h"

#include <opencv2/videoio.hpp>

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <fstream>
#include <functional>
#include <iostream>
#include <memory>
#include <string>
#include <thread>
#include <utility>
#include <vector>

using std::string;
using std::vector;

namespace {

std::string json_escape(const string& s)
{
	std::string out;
	out.reserve(s.size() + 8);
	for (char c : s)
	{
		switch (c)
		{
			case '"': out += "\\\""; break;
			case '\\': out += "\\\\"; break;
			case '\n': case '\r': case '\t': out += ' '; break;
			default:
				if (static_cast<unsigned char>(c) < 0x20)
					out += ' ';
				else
					out += c;
		}
	}
	return out;
}

void emit(const string& line)
{
	std::cout << line << std::endl; // 换行即 flush（行缓冲下也可被管道读到）
}

// 完成一个流时回调：解压落盘 + 输出 JSON 事件
std::function<std::string(const std::string&, const std::vector<uint8_t>&)> make_store(
	const string& outdir, std::shared_ptr<uint64_t> savedCount)
{
	return [outdir, savedCount](const std::string& fallback_name, const std::vector<uint8_t>& data)
	{
		string filename = cimbar::zstd_header_check::get_filename(data.data(), data.size());
		if (!filename.empty())
			filename = File::basename(filename);
		if (filename.empty())
			filename = fallback_name;

		// 文件名里出现非可打印 ASCII 字节说明解码数据出错（zstd 头被误码破坏）。
		// 清洗成 '_' 保证磁盘文件名与 JSON 事件里的路径完全一致——否则
		// server 侧按 UTF-8 替换解码后路径对不上，move 时 FileNotFoundError。
		for (char& c : filename)
			if (static_cast<unsigned char>(c) < 0x20 or static_cast<unsigned char>(c) > 0x7e)
				c = '_';

		string file_path = fmt::format("{}/{}", outdir, filename);
		{
			cimbar::zstd_decompressor<std::ofstream> f(file_path, std::ios::binary);
			f.write(reinterpret_cast<const char*>(data.data()), data.size());
		}
		emit(fmt::format(R"({{"ev":"saved","file":"{}","bytes":{},"path":"{}"}})",
			json_escape(filename), data.size(), json_escape(file_path)));
		++(*savedCount);
		return filename;
	};
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

int fourcc_or_zero(const string& fc)
{
	if (fc.size() != 4)
		return 0;
	return cv::VideoWriter::fourcc(fc[0], fc[1], fc[2], fc[3]);
}

} // namespace

int main(int argc, char** argv)
{
	cxxopts::Options options("vidbar_recv", "vidtx receiver: decode cimbar streams from a capture device or video file");

	options.add_options()
		("i,in", "Device index (e.g. 0) or video file path", cxxopts::value<string>()->default_value("0"))
		("o,out", "Output directory for decoded files", cxxopts::value<string>()->default_value("."))
		("api", "Capture backend: dshow, msmf, v4l2, any", cxxopts::value<string>()->default_value(
#ifdef _WIN32
			"dshow"
#else
			"v4l2"
#endif
		))
		("fourcc", "Pixel format fourcc, e.g. MJPG (empty to skip)", cxxopts::value<string>()->default_value("MJPG"))
		("w,width", "Requested capture width", cxxopts::value<unsigned>()->default_value("1920"))
		("height", "Requested capture height", cxxopts::value<unsigned>()->default_value("1080"))
		("F,fps", "Requested capture fps (0 = don't set)", cxxopts::value<unsigned>()->default_value("0"))
		("m,mode", "cimbar mode [B,Bm,Bu,4C]", cxxopts::value<string>()->default_value("B"))
		("c,ccm", "Color correction mode (2=auto header-based, 0=off)", cxxopts::value<int>()->default_value("2"))
		("s,stats", "Print a stat event every N frames, and at least every ~2 seconds (0 = off)", cxxopts::value<unsigned>()->default_value("120"))
		("split", "Split each frame into N vertical strips, decoding one code per strip "
		          "(2 = two side-by-side windows on ONE captured screen; 0 = auto-detect, default)",
			cxxopts::value<unsigned>()->default_value("0"))
		("h,help", "Print usage")
	;
	options.parse_positional({"in", "out"});
	options.positional_help("<in> <out>");

	auto result = options.parse(argc, argv);
	if (result.count("help"))
	{
		std::cout << options.help() << std::endl;
		return 0;
	}

	string source = result["in"].as<string>();
	string outdir = result["out"].as<string>();
	string api = result["api"].as<string>();
	string fourcc = result["fourcc"].as<string>();
	unsigned width = result["width"].as<unsigned>();
	unsigned height = result["height"].as<unsigned>();
	unsigned wantFps = result["fps"].as<unsigned>();
	unsigned statsEvery = result["stats"].as<unsigned>();
	int colorCorrection = result["ccm"].as<int>();
	unsigned wantSplit = result["split"].as<unsigned>();
	if (wantSplit > 4)
	{
		emit(R"({"ev":"error","msg":"--split must be 0..4"})");
		return 64;
	}
	unsigned strips = wantSplit ? wantSplit : 2; // 0 = 自动：先按双码流探测

	cimbar::Config::update(parse_mode(result["mode"].as<string>()));

	// 打开视频源：纯数字 -> 设备索引 + 指定后端；否则当作文件路径
	cv::VideoCapture vc;
	bool isDevice = !source.empty() and source.find_first_not_of("0123456789") == string::npos;
	int apiPref = cv::CAP_ANY;
	if (api == "dshow") apiPref = cv::CAP_DSHOW;
	else if (api == "msmf") apiPref = cv::CAP_MSMF;
	else if (api == "v4l2") apiPref = cv::CAP_V4L2;

	if (isDevice)
	{
		// 格式自动协商：不少采集卡驱动会悄悄把 MJPG 回落成 YUY2（无压缩，
		// 带宽需求几十倍于 MJPG，实际帧率会从 60 掉到个位数）。常见规律是
		// 1080p 只有 YUY2、MJPG 只支持到 720p —— 所以除了换后端、降帧率，
		// 还要降分辨率试。每一步都发 negotiate 事件，绝不静默（否则像卡死）。
		int devIdx = static_cast<int>(std::stoul(source));
		int fc = fourcc_or_zero(fourcc);
		int altPref = cv::CAP_ANY;
		string altName;
#ifdef _WIN32
		if (apiPref == cv::CAP_DSHOW) { altPref = cv::CAP_MSMF; altName = "msmf"; }
		else if (apiPref == cv::CAP_MSMF) { altPref = cv::CAP_DSHOW; altName = "dshow"; }
#endif
		auto fourccStr = [](uint32_t v)
		{
			string s;
			for (unsigned i = 0; i < 4; ++i)
			{
				char c = static_cast<char>((v >> (8 * i)) & 0xFF);
				s += (c >= 0x20 and c <= 0x7e) ? c : '?';
			}
			return s;
		};

		struct Attempt { int api; string apiName; unsigned w, h, fps; };
		vector<Attempt> attempts;
		if (!fc)
		{
			attempts.push_back({apiPref, api, width, height, wantFps});
		}
		else
		{
			auto addBackend = [&](int a, const string& an)
			{
				Attempt cand[] = {
					{a, an, width, height, wantFps},
					{a, an, 1280, 720, wantFps},
				};
				for (auto& t : cand)
					if (std::find_if(attempts.begin(), attempts.end(), [&](const Attempt& x)
						{ return x.api == t.api and x.w == t.w and x.h == t.h
							and x.fps == t.fps; }) == attempts.end())
						attempts.push_back(t);
			};
			addBackend(apiPref, api);
			if (altPref != cv::CAP_ANY)
				addBackend(altPref, altName);
		}

		auto applyFormat = [&](unsigned w, unsigned h, unsigned fpsTry)
		{
			if (fc)
				vc.set(cv::CAP_PROP_FOURCC, fc);
			vc.set(cv::CAP_PROP_FRAME_WIDTH, w);
			vc.set(cv::CAP_PROP_FRAME_HEIGHT, h);
			if (fpsTry)
				vc.set(cv::CAP_PROP_FPS, fpsTry);
			// 有些驱动改分辨率/帧率时丢 FOURCC，重设一遍再读回校验。
			if (fc)
			{
				vc.set(cv::CAP_PROP_FOURCC, fc);
				if (fpsTry)
					vc.set(cv::CAP_PROP_FPS, fpsTry);
			}
		};

		bool negotiated = false;
		for (auto& t : attempts)
		{
			emit(fmt::format(R"({{"ev":"negotiate","msg":"trying {} {}x{}@{}"}})",
				json_escape(t.apiName), t.w, t.h, t.fps));
			vc.open(devIdx, t.api);
			if (!vc.isOpened())
			{
				emit(R"({"ev":"negotiate","msg":"open failed, next"})");
				vc.release();
				std::this_thread::sleep_for(std::chrono::milliseconds(300));
				continue;
			}
			applyFormat(t.w, t.h, t.fps);
			uint32_t got = static_cast<uint32_t>(vc.get(cv::CAP_PROP_FOURCC));
			string gotStr = fourccStr(got);
			// msmf 后端往往不回报 FOURCC（读回 0 -> "????"）：无法校验，
			// 只能当成功接受，格式好坏由后续 readfps 诊断判定。
			bool unknown = gotStr.find('?') != string::npos;
			if (!fc or got == static_cast<uint32_t>(fc) or unknown)
			{
				if (unknown)
					emit(R"({"ev":"negotiate","msg":"backend does not report fourcc, assuming ok"})");
				negotiated = true;
				break;
			}
			emit(fmt::format(R"({{"ev":"negotiate","msg":"got '{}' instead of '{}', next"}})",
				json_escape(gotStr), json_escape(fourcc)));
			vc.release();
			// 驱动释放设备需要时间，立刻重开可能挂起
			std::this_thread::sleep_for(std::chrono::milliseconds(300));
		}
		if (!negotiated)
		{
			// 协商链全失败：按原始参数开一次，能开就先凑合用。
			vc.open(devIdx, apiPref);
			if (!vc.isOpened())
			{
				emit(fmt::format(R"({{"ev":"error","msg":"failed to open source '{}'"}})", json_escape(source)));
				return 70;
			}
			applyFormat(width, height, wantFps);
		}
	}
	else
		vc.open(source, cv::CAP_FFMPEG);

	if (!vc.isOpened())
	{
		emit(fmt::format(R"({{"ev":"error","msg":"failed to open source '{}'"}})", json_escape(source)));
		return 70;
	}

	double realW = vc.get(cv::CAP_PROP_FRAME_WIDTH);
	double realH = vc.get(cv::CAP_PROP_FRAME_HEIGHT);
	double realFps = vc.get(cv::CAP_PROP_FPS);
	// fourcc 以整数值 double 返回（见 OpenCV videowriter_basic 示例），
	// 必须先转 int 再按小端拆字节；直接按 double 内存拆只会得到低尾零字节。
	uint32_t fcInt = static_cast<uint32_t>(vc.get(cv::CAP_PROP_FOURCC));
	string fcStr;
	for (unsigned i = 0; i < 4; ++i)
	{
		char c = static_cast<char>((fcInt >> (8 * i)) & 0xFF);
		fcStr += (c >= 0x20 and c <= 0x7e) ? c : '?';
	}

	emit(fmt::format(R"({{"ev":"open","source":"{}","device":{},"w":{},"h":{},"fps":{},"fourcc":"{}","split":{}}})",
		json_escape(source), isDevice ? 1 : 0, (int)realW, (int)realH, realFps, json_escape(fcStr), wantSplit));

	unsigned chunkSize = cimbar::Config::fountain_chunk_size();
	auto savedCount = std::make_shared<uint64_t>(0);
	// 一个 sink 收所有流：分片头部带 encode_id，双窗口两条码流天然互不混淆。
	fountain_decoder_sink sink(chunkSize, make_store(outdir, savedCount));

	// 每个竖条一条独立解码管线（角点跟踪/色彩校正等有状态，不能共享）。
	// 多码流模式额外加一条整帧管线（下标 strips）：条带提不出角点时回退整帧解码，
	// 使得「单窗口 / 双窗口」无需用户配置即可自动适配。
	vector<std::unique_ptr<Extractor>> exts;
	vector<std::unique_ptr<Decoder>> decs;
	unsigned pipelines = strips + (strips >= 2 ? 1 : 0);
	for (unsigned i = 0; i < pipelines; ++i)
	{
		exts.emplace_back(new Extractor());
		decs.emplace_back(new Decoder());
	}

	cv::Mat mat;
	uint64_t frames = 0, decoded = 0;
	uint64_t stripExtOk = 0, stripDecOk = 0, fullExtOk = 0, fullDecOk = 0;
	auto t0 = std::chrono::high_resolution_clock::now();
	auto lastStatT = t0;
	auto lastSignalT = t0;
	auto lastDecodeT = t0;
	bool hasDecoded = false;
	bool locked = false;
	unsigned consecutiveReadFail = 0;
	// 码流布局自适应：0=未定 1=条带 2=整帧。谁先解出数据锁定谁（省 CPU）；
	// 长期提不出角点则重置重探——用户中途增减窗口也能自愈。
	int layoutPrefer = 0;
	uint64_t framesSinceExtract = 0;

	while (true)
	{
		if (!vc.read(mat) or mat.empty())
		{
			// 文件读完 -> 正常结束；设备瞬时读失败 -> 短暂退避后重试
			if (!isDevice)
				break;
			if (++consecutiveReadFail > 600) // ~6s 仍读不到帧
			{
				emit(R"({"ev":"error","msg":"device read failed repeatedly"})");
				return 71;
			}
			std::this_thread::sleep_for(std::chrono::milliseconds(10));
			continue;
		}
		consecutiveReadFail = 0;
		++frames;

		cv::UMat img = mat.getUMat(cv::ACCESS_RW).clone();

		// --split：一帧竖切 N 条，每条独立提角点+解码（同屏并排多码流）。
		// 自适应：条带全提不出角点时回退整帧管线；布局锁定后跳过另一侧省 CPU。
		bool frameGood = false;
		int decodedBytes = 0;
		bool stripDecoded = false;
		bool stripExtracted = false;
		if (strips == 1 or layoutPrefer == 2)
		{
			// 单码流：整帧解码（strips>=2 时用专用的整帧管线，下标 strips）
			unsigned fullIdx = strips >= 2 ? strips : 0;
			cv::UMat out;
			int res = exts[fullIdx]->extract(img, out);
			if (res)
			{
				frameGood = true;
				++fullExtOk;
				int nbytes = decs[fullIdx]->decode_fountain(out, sink,
					res == Extractor::NEEDS_SHARPEN, colorCorrection);
				if (nbytes > 0)
				{
					++decoded;
					decodedBytes += nbytes;
					++fullDecOk;
				}
			}
		}
		else
		{
			for (unsigned s = 0; s < strips; ++s)
			{
				cv::UMat out;
				int x0 = static_cast<int>(static_cast<long long>(mat.cols) * s / strips);
				int x1 = static_cast<int>(static_cast<long long>(mat.cols) * (s + 1) / strips);
				cv::UMat strip = img(cv::Rect(x0, 0, x1 - x0, mat.rows));
				int res = exts[s]->extract(strip, out);
				if (!res)
					continue;
				frameGood = true;
				stripExtracted = true;
				++stripExtOk;
				int nbytes = decs[s]->decode_fountain(out, sink,
					res == Extractor::NEEDS_SHARPEN, colorCorrection);
				if (nbytes > 0)
				{
					++decoded;
					decodedBytes += nbytes;
					stripDecoded = true;
				}
			}
			// 整帧回退：布局未定，或条带全提不出角点（码流跨条带边界/单码流居中）
			if (layoutPrefer != 1 or !stripExtracted)
			{
				cv::UMat out;
				int res = exts[strips]->extract(img, out);
				if (res)
				{
					frameGood = true;
					++fullExtOk;
					int nbytes = decs[strips]->decode_fountain(out, sink,
						res == Extractor::NEEDS_SHARPEN, colorCorrection);
					if (nbytes > 0)
					{
						++decoded;
						decodedBytes += nbytes;
						++fullDecOk;
					}
				}
			}
		}

		// 布局锁定与自愈
		if (decodedBytes > 0)
			layoutPrefer = stripDecoded ? 1 : 2;
		if (frameGood)
		{
			framesSinceExtract = 0;
		}
		else if (++framesSinceExtract > 300 and layoutPrefer != 0)
		{
			layoutPrefer = 0; // ~10s 无角点：重探布局（窗口被移动/增减）
			framesSinceExtract = 0;
		}

		if (std::getenv("VIDBAR_DEBUG"))
			std::cerr << fmt::format("[dbg] frame={} strips={} prefer={} extract={} bytes={}",
				frames, strips, layoutPrefer, frameGood, decodedBytes) << std::endl;

		// 信号锁定判定（带迟滞）：
		//   成为锁定：必须解出过数据帧（蓝屏/无信号画面骗不过解码器）；
		//   维持锁定：角点提取成功即可（容忍瞬时解码失败）。
		auto now = std::chrono::high_resolution_clock::now();
		if (frameGood)
			lastSignalT = now;
		if (decodedBytes > 0)
		{
			lastDecodeT = now;
			hasDecoded = true;
		}
		bool extractRecent = std::chrono::duration_cast<std::chrono::milliseconds>(now - lastSignalT).count() < 2000;
		bool decodeRecent = hasDecoded and
			std::chrono::duration_cast<std::chrono::milliseconds>(now - lastDecodeT).count() < 5000;
		bool nowLocked = locked ? extractRecent : decodeRecent;
		if (nowLocked != locked)
		{
			locked = nowLocked;
			emit(fmt::format(R"({{"ev":"signal","locked":{}}})", locked ? "true" : "false"));
		}

		// 统计按时间触发（≥2 秒一次）：实际帧率很低时按帧数触发会长时间无输出，
		// 而低帧率恰恰是最需要看到 readfps 诊断的时刻。
		if (statsEvery and (frames % statsEvery == 0 or
			std::chrono::duration_cast<std::chrono::milliseconds>(now - lastStatT).count() >= 2000))
		{
			lastStatT = now;
			double secs = std::chrono::duration_cast<std::chrono::milliseconds>(now - t0).count() / 1000.0;
			string progress;
			for (double p : sink.get_progress())
				progress += (progress.empty() ? "" : ",") + fmt::format("{:.3f}", p);
			emit(fmt::format(R"({{"ev":"stat","frames":{},"decoded":{},"saved":{},"readfps":{:.1f},"streams":{},"progress":[{}],"prefer":{},"sext":{},"sdec":{},"fext":{},"fdec":{}}})",
				frames, decoded, *savedCount, frames / (secs > 0 ? secs : 1), sink.num_streams(), progress,
				layoutPrefer, stripExtOk, stripDecOk, fullExtOk, fullDecOk));
		}
	}

	emit(fmt::format(R"({{"ev":"exit","frames":{},"decoded":{},"saved":{}}})", frames, decoded, *savedCount));
	return 0;
}
