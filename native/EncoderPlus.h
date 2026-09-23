/* This code is subject to the terms of the Mozilla Public License, v2.0. http://mozilla.org/MPL/2.0/. */
/* vidtx 定制补丁：基于上游 EncoderPlus.h，修复小文件喷泉流卡死问题。
 *
 * 上游原实现：
 *   unsigned requiredFrames = fes->blocks_required() * redundancy / chunks_per_frame;
 * 整数截断会把 1.6 帧砍成 1 帧 —— 小文件（manifest / 单个小分片）每轮只
 * 渲染唯一 1 帧。wirehair 种子确定、同一文件每轮产生相同的块，接收端一旦
 * 丢掉这帧里的个别块（真机信道必然有误码），rank 永远补不齐，流会精确
 * 卡死（实测 80%）且跨轮/重试均无法自愈。
 *
 * 修复：帧数向上取整，并给小流保底 8 帧 —— 每帧从喷泉流取走的是不同的
 * 块，多帧才有个别块解码失败时的汇聚余量。大文件帧数不受影响。
 */
#pragma once

#include "Encoder.h"
#include "cimb_translator/Config.h"
#include "extractor/Scanner.h"
#include "serialize/format.h"
#include "util/File.h"

#include <opencv2/opencv.hpp>
#include <cmath>
#include <filesystem>
#include <functional>
#include <string>

class EncoderPlus : public Encoder
{
public:
	using Encoder::Encoder;

	unsigned encode(const std::string& filename, std::string output_prefix);
	unsigned encode_fountain(const std::string& filename, std::string output_prefix, int compression_level=16, double redundancy=1.2);
	unsigned encode_fountain(const std::string& filename, const std::function<bool(const cv::Mat&, unsigned)>& on_frame, int compression_level=16, double redundancy=4.0);
};

inline unsigned EncoderPlus::encode(const std::string& filename, std::string output_prefix)
{
	std::ifstream f(filename, std::ios::binary);

	unsigned i = 0;
	while (true)
	{
		auto frame = encode_next(f);
		if (!frame)
			break;

		std::string output = fmt::format("{}_{}.png", output_prefix, i);
		// imwrite expects BGR
		cv::cvtColor(*frame, *frame, cv::COLOR_RGB2BGR);
		cv::imwrite(output, *frame);
		++i;
	}
	return i;
}

inline unsigned EncoderPlus::encode_fountain(const std::string& filename, const std::function<bool(const cv::Mat&, unsigned)>& on_frame, int compression_level, double redundancy)
{
	std::ifstream infile(filename, std::ios::binary);
	fountain_encoder_stream::ptr fes = create_fountain_encoder(infile, File::basename(filename), compression_level);
	if (!fes)
		return 0;

	// ex: with ecc = 30 and 155 byte blocks, we have 60 rs blocks * 125 bytes per block == 7500 bytes to work with.
	// if fountain_chunks_per_frame() is 10, the fountain_chunk_size will be 750.
	// we calculate requiredFrames based only on symbol bits, to avoid the situation where the color decode is failing while we're
	// refusing to generate additional frames...
	//
	// vidtx: 向上取整 + 小流保底（见文件头注释）。
	unsigned requiredFrames = static_cast<unsigned>(
		std::ceil(fes->blocks_required() * redundancy / cimbar::Config::fountain_chunks_per_frame(_bitsPerSymbol)));
	if (requiredFrames < 8)
		requiredFrames = 8;

	unsigned i = 0;
	unsigned consecutiveScansFailed = 0;
	while (i < requiredFrames)
	{
		auto frame = encode_next(*fes);
		if (!frame)
			break;

		// some % of generated frames (for the current 8x8 impl)
		// will produce random patterns that falsely match as
		// corner "anchors" and fail to extract. So:
		// if frame fails the scan, skip it.
		if (!Scanner::will_it_scan(*frame))
		{
			if (++consecutiveScansFailed < 5)
				continue;

			// else, we gotta make forward progress. And it's probably a bug?
			std::cerr << fmt::format("generated {} bad frames in a row. This really shouldn't happen, maybe report a bug. :(", consecutiveScansFailed) << std::endl;
		}

		consecutiveScansFailed = 0;
		if (!on_frame(*frame, i))
			break;
		++i;
	}
	return i;
}

inline unsigned EncoderPlus::encode_fountain(const std::string& filename, std::string output_prefix, int compression_level, double redundancy)
{
	std::function<bool(const cv::Mat&, unsigned)> fun = [output_prefix] (const cv::Mat& frame, unsigned i) {
		std::string output = fmt::format("{}_{}.png", output_prefix, i);
		cv::Mat bgr;
		cv::cvtColor(frame, bgr, cv::COLOR_RGB2BGR);
		return cv::imwrite(output, bgr);
	};
	return encode_fountain(filename, fun, compression_level, redundancy);
}
