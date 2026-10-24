# 17. 표준 요구사항 추적표 (표준 → 코드 → 시험)

> `05-standards.md`와 `docs/modules/iptv.md`의 **필수** 요구사항이 코드 어디에 구현되고 어떤 자동 시험이 증명하는지 적는다.
> 상태: ✅ 구현 + 자동 시험 / ◐ 일부 / ⬜ 아직(예정 단계). 코드·시험 이름은 `tests/test_traceability.py`가 실제로 있는지 검사한다(표가 코드와 어긋나면 시험 실패).
> 규칙: 표준을 반영하는 코드를 바꾸면 이 표도 같이 고친다. ⬜ 항목을 구현하면 시험을 붙이고 ✅로 바꾼다.

## 1. 전송·형식
| 요구 | 근거 | 상태 | 코드 | 시험 |
|------|------|------|------|------|
| RTP 헤더 해석(순번·타임스탬프·SSRC·CSRC·확장·패딩) | RFC 3550 5.1 | ✅ | `core/blocks/rtp_header.py` | `tests/blocks/test_ts_analyzer.py::test_rtp_header_parse` |
| 페이로드 타입 33(MP2T), 90 kHz | RFC 3551 | ✅ 송신 / 수신 PT 검사는 아래 줄 | `modules/iptv/sender/commands.py` | `tests/iptv/test_sender.py::test_send_command_uses_confirmed_options` |
| 수신: PT ≠ 33은 계측에서 빼고 따로 셈, M 비트(타임스탬프 불연속)면 지터 기준 새로 | RFC 3551, RFC 2250 2.1, 05 2.2·3.2 | ✅ (2026-09-25 전수 검토에서 찾음, 20 GAP-41). 따로 센 수의 보고는 `non_video_traffic_bps`(P3) | `modules/iptv/receiver/analyzer.py`, `core/blocks/jitter_calc.py` | `tests/iptv/test_analyzer.py::test_other_payload_types_are_kept_out_of_the_channel` |
| IGMPv3 GMI 270 s(RFC 9776)·v2 260 s로 가입 만료 원인 판단 | RFC 2236 8.4, RFC 9776 8.4, 05 1.11 | ⬜ P4 원인 분석(R-02) | | |
| 데이터그램당 TS 7개(1316 B), 단편화 없음 | RFC 2250, RFC 8085 | ✅ 송신 / 수신은 188 배수 아니면 sync 오류 | `modules/iptv/sender/commands.py`, `core/blocks/ts_analyzer.py` | `tests/iptv/test_sender.py::test_send_receive_clean` |
| SSRC = 채널(위 8비트) + 송출마다 난수 | iptv 2장 | ✅ | `modules/iptv/sender/commands.py` | `tests/iptv/test_sender.py::test_ssrc_keeps_channel_in_top_bits` |
| CBR TS, 널 패킷 채움, muxrate 고정 | 05 3.1, ISO 13818-1 | ✅ | `modules/iptv/sender/commands.py` | `tests/iptv/test_sender.py::test_prepare_command_per_quality` |
| 정속 송출, 반복 이음새(CC·PCR 연속) | 05 3.1, iptv 4.2·4.4 | ✅ | `modules/iptv/sender/commands.py`, `modules/iptv/sender/session.py` | `tests/iptv/test_sender.py::test_send_receive_clean` |
| 자동 생성 영상: 채널 이름·채널별 소리 높이·서비스 ID = 채널, 채널·화질·비트레이트별 보관, `.part` → 검사 후 이름 바꿈, 차례로 하나씩, 60초 무진행·디스크·ffmpeg 오류 = SND-020, 취소 = USR-006 | iptv 1-1, 1-1-2, 2장, 09 SND-020·USR-006 | ✅ | `modules/iptv/sender/prepare.py`, `modules/iptv/sender/commands.py`, `modules/iptv/module.py` | `tests/iptv/test_prepare.py::test_makes_checks_keeps_then_reuses`, `tests/iptv/test_prepare.py::test_a_changed_description_means_make_again`, `tests/iptv/test_prepare.py::test_cancel_removes_the_file_being_made`, `tests/iptv/test_prepare.py::test_no_progress_fails_and_stops_the_program`, `tests/iptv/test_prepare.py::test_not_enough_disk_fails_before_starting`, `tests/iptv/test_prepare.py::test_ffmpeg_error_is_reported`, `tests/iptv/test_prepare.py::test_a_left_part_file_is_removed_at_start`, `tests/iptv/test_prepare.py::test_monkey_requests_and_cancels` |
| 준비 파일 검사: TR 101 290 P1 0(CBR 이론 시각), 서비스·PID, PCR 비트레이트 ±0.5%, PCR 간격 ≤ 40 ms, **모든 프레임의 표식이 (채널, 0, 1, 2, …)**(2026-09-26 W-7) | iptv 4.3, 3.4, 3장 | ✅ | `modules/iptv/sender/prepare.py`, `core/blocks/ts_analyzer.py`, `modules/iptv/marker.py` | `tests/iptv/test_prepare.py::test_good_file_passes_the_check`, `tests/iptv/test_prepare.py::test_wrong_channel_or_rate_is_caught`, `tests/iptv/test_prepare.py::test_a_lost_packet_is_caught`, `tests/iptv/test_prepare.py::test_marker_check_passes_a_good_file_and_catches_wrong_ones`, `tests/iptv/test_prepare.py::test_a_wrong_marker_fails_the_file_and_leaves_nothing` |
| 프레임 번호 표식(숫자 + 막대 22칸: 기준·채널 2비트·프레임 16비트) 새기기·읽기, 작은 캡처에서도, 추측 안 함, 막대 영역 ROI(최고 화질, AQ 명시 — B-프레임이 뒤 프레임 표식을 복사하지 않게, W-7) | iptv 3.4, 3장, 04 V12 | ✅ | `modules/iptv/marker.py`, `modules/iptv/sender/commands.py` | `tests/iptv/test_marker.py::test_cell_sizes`, `tests/iptv/test_marker.py::test_every_frame_reads_back_in_order`, `tests/iptv/test_marker.py::test_small_capture_still_reads`, `tests/iptv/test_marker.py::test_uhd_reads`, `tests/iptv/test_marker.py::test_no_marker_is_none`, `tests/iptv/test_marker.py::test_broken_end_reference_is_none`, `tests/iptv/test_marker.py::test_random_pictures_never_crash` |
| 시작 때 없는 영상 만들기(진행률 `preparing`, 이때만 Cancel), 파일 없으면 자동 생성으로 대체(SND-001 ⚠), 미리 만들기(`zeroloss prepare`), 송신 3채널 동시(UHD) | iptv 1-1-2, 02 2-1·2-4·5, 11-ui 1.10, 13 | ✅ | `controller/run.py`, `controller/service.py`, `controller/cli.py`, `controller/ui/control_state.py`, `modules/iptv/run.py`, `core/node.py` | `tests/iptv/test_run_controller.py::test_plan_without_files_makes_the_video_then_runs`, `tests/iptv/test_run_controller.py::test_missing_prepared_file_falls_back_to_the_generated_video`, `tests/iptv/test_run_controller.py::test_cancel_while_the_video_is_made`, `tests/iptv/test_run_controller.py::test_video_that_cannot_be_made_fails_the_start`, `tests/iptv/test_run_controller.py::test_prepare_ahead_then_three_uhd_channels_send_together`, `tests/iptv/test_run_controller.py::test_node_shutdown_stops_a_video_being_made`, `tests/iptv/test_run_controller.py::test_failed_start_leaves_the_last_run_alone`, `tests/iptv/test_cli.py::test_prepare_from_the_cli`, `tests/controller_ui/test_control_state.py::test_cancel_while_videos_are_made` |
| 송출 안정(±1% 3초) 후 수신 가입 | 05 2.1-2 규칙 1 | ✅ 송신 판정 + Controller 단계 순서 | `modules/iptv/run.py`, `controller/run.py` | `tests/iptv/test_run_rules.py::test_start_order_senders_then_receivers_and_reverse_stop` |
| TTL 16(최소 8), 송출 NIC 지정 | 05 3.1·3.2 | ◐ 옵션·최소값 검사(S-15 8~64) ✅ / 시험망 캡처 확인 ⬜ | `modules/iptv/sender/commands.py` | `tests/iptv/test_sender.py::test_send_command_uses_confirmed_options` |
| 채널 그룹 주소끼리 MAC 겹침 금지, 예약 MAC 겹침 금지 | RFC 1112, 05 3.3 | ✅ 계획 검사 | `modules/iptv/run.py` | `tests/iptv/test_run_rules.py::test_group_address_rules` |
| 송신 1초 기록(보낸 수·NIC 전송 카운터·최대 공백) | 05 3.4·3.4-1 | ◐ 1초 비트레이트만 ✅ / 보낸 수·NIC·공백 ⬜ | `modules/iptv/sender/session.py` | `tests/iptv/test_sender.py::test_send_receive_clean` |
| RTCP 보내지 않음 | 05 3.2·4.1 | ✅ 송신 명령에 RTCP 없음, 수신은 받기만 | `modules/iptv/sender/commands.py`, `core/blocks/udp_receiver.py` | `tests/iptv/test_sender.py::test_send_command_uses_confirmed_options` |

## 2. 멀티캐스트 가입
| 요구 | 근거 | 상태 | 코드 | 시험 |
|------|------|------|------|------|
| 가입·탈퇴는 OS 소켓 기능(IGMP를 직접 만들지 않음) | RFC 2236/9776, 05 4.1 | ✅ | `core/blocks/udp_receiver.py` | `tests/blocks/test_udp_receiver.py::test_random_join_leave_send_matches_membership` |
| 채널 전환 = Leave 후 Join | RFC 2236, 05 4.1 | ✅ 블록 + PC 수신기(분석 소켓·재생기 함께 닫고 Leave → Join, 2026-10-01) | `core/blocks/udp_receiver.py`, `modules/iptv/receiver/session.py` | `tests/blocks/test_udp_receiver.py::test_channel_change_leave_then_join`, `tests/iptv/test_receiver_switch.py::test_switch_keeps_real_time_clock_and_writes_the_zap` |
| Leave 뒤 이전 그룹이 3초 넘게 오면 NET-005(원시 소켓 4초, 권한 없음·같은 노드가 그룹 유지 = `not_checked`) | 03 F19, 09 NET-005, RFC 2236 3 | ✅ PC 수신기 / 실제 공유기 확인 ⬜(실기) | `core/blocks/leave_watch.py`, `modules/iptv/receiver/session.py` | `tests/blocks/test_leave_watch.py::test_packet_after_three_seconds_is_not_processed`, `tests/iptv/test_receiver_switch.py::test_leave_check_marks_a_router_that_keeps_sending`, `tests/iptv/test_receiver_switch.py::test_held_group_is_not_checked` |
| 소켓은 자기가 가입한 그룹만 받음 | 05 4.2.2 | ✅ | `core/blocks/udp_receiver.py` | `tests/blocks/test_udp_receiver.py::test_socket_only_gets_its_own_group` |
| 실제 쓰인 IGMP 버전 기록 | 05 1.2 IGMP 방식, V15 | ⬜ P1·P3 | | |
| IPv6(MLD) | RFC 2710/9777, 05 1.7 | ⬜ 적용 범위대로(1차 제외) | | |

## 3. 손실·지터
| 요구 | 근거 | 상태 | 코드 | 시험 |
|------|------|------|------|------|
| 확장 순번, 예상 수 = 최대 − 첫 + 1 | RFC 3550 A.1·A.3 | ✅ | `core/blocks/seq_tracker.py` | `tests/blocks/test_seq_tracker.py::test_sequence_and_timestamp_wraparound` |
| 중복·순서 뒤바뀜은 손실과 따로 | RFC 3550 A.3, RFC 3611 | ✅ | `core/blocks/seq_tracker.py` | `tests/blocks/test_seq_tracker.py::test_duplicates_are_not_loss` |
| 정렬 창 1,000 ms: 안에서 정리 = 오류 아님, 넘으면 늦음 = 손실 | RFC 7003, 05 2.1 | ✅ | `core/blocks/seq_tracker.py` | `tests/blocks/test_seq_tracker.py::test_arrival_after_window_is_late_and_lost` |
| 연속 손실(버스트) 길이 | RFC 3357, RFC 6958 | ✅ 길이 / 갭(버스트 사이 간격) 통계 ⬜ | `core/blocks/seq_tracker.py` | `tests/blocks/test_seq_tracker.py::test_scattered_and_burst_loss` |
| SSRC 변경 = 새 구간, 같은 SSRC 점프 = 손실 | 05 2.1 | ✅ | `core/blocks/seq_tracker.py` | `tests/blocks/test_seq_tracker.py::test_ssrc_change_is_new_segment_not_loss` |
| 긴 끊김(순번 한 바퀴 이상) | 05 2.1 | ✅ | `core/blocks/seq_tracker.py` | `tests/blocks/test_seq_tracker.py::test_long_outage_longer_than_one_seq_cycle` |
| Interarrival Jitter J += (\|D\| − J)/16, ms | RFC 3550 6.4.1 | ✅ | `core/blocks/jitter_calc.py` | `tests/blocks/test_jitter_calc.py::test_matches_rfc_reference` |
| 타임스탬프 되돌아감 | RFC 3550, RUN-001 | ✅ | `core/blocks/jitter_calc.py` | `tests/blocks/test_jitter_calc.py::test_timestamp_wraparound_is_not_a_spike` |
| 커널 수신 시각 | 05 4.4 | ✅ Linux / Windows는 단조 시계(보고서 표시 ⬜) | `core/blocks/udp_receiver.py` | `tests/blocks/test_udp_receiver.py::test_kernel_time_and_own_drops_add_up` |
| IPDV, MDI DF·MLR | RFC 3393/5481, RFC 4445 | ⬜ P2 보조 표시 | | |
| RTP 없는 모드: CC로 손실, 16배수 한계 표시 | 05 2.1, TR 101 290 1.4 | ◐ CC 오류 ✅ / 손실 수로 환산 ⬜ | `core/blocks/ts_analyzer.py` | `tests/blocks/test_ts_analyzer.py::test_random_video_drops_counted_as_cc_errors` |
| 입력 형식 자동 판별(첫 바이트 0x47), 설정과 다르면 RCV-022 / 이 빌드는 S-10 RTP만(RUN-013) | TS 102 034 7.1.3, 16 3 | ✅ (2026-09-25) | `modules/iptv/receiver/analyzer.py`, `modules/iptv/run.py` | `tests/iptv/test_analyzer.py::test_ts_without_rtp_is_detected` |
| 누적 카운터 64비트 | 09 0.3-8 | ✅ Python 정수 | `core/blocks/rate_meter.py` | `tests/blocks/test_rate_and_recorder.py::test_counters_do_not_overflow_past_32_bits` |

## 3-1. 알림·문제 (F21)
| 요구 | 근거 | 상태 | 코드 | 시험 |
|------|------|------|------|------|
| 09의 모든 코드 What/Why/Fix, 코드+장비 한 줄, 되풀이(10 s)·폭주(10분 10개)·오래됨(24 h), clears·시험 끝 해결, 해결 줄 500개까지 | 03 F21, ISA-18.2, 02 2-2·8 | ✅ 저장소·창구·배지·창 / 버튼·보고서 표 ⬜ | `controller/problems/__init__.py`, `controller/problems/catalog.json` | `tests/controller/test_problems.py::test_random_events_keep_invariants_and_memory_bounded` |
| 사람 할 일: 문제 코드로 열림, 풀리면 자동 ✔, 감지 못 하는 일만 [Done], 경험 DB에 행동 기록, 창은 따로·ZeroLoss 창 위(다른 프로그램 위 아님) | 03 F22, 14 1, 02 2-3 | ✅ / 그림·원격 웹 ⬜ | `controller/actions/__init__.py`, `controller/actions/catalog.json` | `tests/controller/test_actions.py::test_problem_opens_an_action_and_its_clear_closes_it` |
| 모든 사건 → 경험 DB situation(Controller) | 14 1 | ✅ (2026-09-25, 20 GAP-46) | `controller/service.py` | `tests/iptv/test_run_controller.py::test_controller_records_events_in_the_experience_db` |
| 남는 프로세스 없음: 한 폴더 기록, 노드 시작 때 정리(SYS-007), kill -9 때 자식도 죽음, 시험 끝 토큰 프로세스 0 | 12 4-3, 09 SYS-007 | ✅ Linux / Windows Job Object ⬜(V18) | `core/blocks/process_runner.py`, `core/node.py` | `tests/core/test_leftovers.py::test_killed_node_leaves_no_tsp_or_vlc` |
| Controller 재시작 복구: 옛 시험 즉시 정지(GET에서도 새 boot 확인), 요약 회수, 복구 보고서 | 03 F10·F11, 09 CTL-003, 02 3 | ✅ | `controller/run.py`, `core/protocol/http_server.py` | `tests/iptv/test_run_controller.py::test_controller_crash_is_recovered_on_the_next_start` |
| 디스크 부족: 1분마다 확인, 1 GB 미만 경고(해제 포함), 200 MB 미만 노드 안전 정지 → 시험 전체 정상 종료·보고서(INCOMPLETE, `disk_low: <장비>`) | 03 F10, 09 RCV-008, 02 10 | ✅ | `core/node.py`, `controller/run.py` | `tests/core/test_node_recovery.py::test_disk_low_warns_then_stops_the_node`, `tests/iptv/test_run_controller.py::test_disk_full_on_a_node_ends_the_test_with_a_report` |
| 노드 재시작 복구: 열린 기록 파일 닫기, 5분 요약 디스크 보관 | 03 F12, 02 4 | ✅ | `modules/iptv/module.py`, `core/node.py` | `tests/core/test_node_recovery.py::test_summaries_survive_a_restart` |

## 4. MPEG-TS (TR 101 290)
| 요구 | 근거 | 상태 | 코드 | 시험 |
|------|------|------|------|------|
| 1.1 sync loss, 1.2 sync byte | TR 101 290 | ✅ | `core/blocks/ts_analyzer.py` | `tests/blocks/test_ts_analyzer.py::test_sync_byte_and_sync_loss` |
| 1.3.a PAT_error_2(table_id 0x00 구간 0.5 s·다른 table·스크램블) | TR 101 290 V1.4.1 | ✅ (2026-09-25, 1.3 대체) | `core/blocks/ts_analyzer.py` | `tests/blocks/test_ts_analyzer.py::test_pid0_packets_without_a_pat_section_are_a_pat_error` |
| 1.4 CC(페이로드 없으면 증가 금지, 중복 1회는 같은 패킷만 — PCR만 달라도 됨 13818-1 2.4.3.3) | ISO 13818-1, TR 101 290 | ✅ | `core/blocks/ts_analyzer.py` | `tests/blocks/test_ts_analyzer.py::test_cc_loss_order_duplicates_and_discontinuity` |
| 1.5.a PMT_error_2(각 program_map_PID의 0x02 구간 0.5 s) | TR 101 290 V1.4.1 | ✅ (2026-09-25, 1.5 대체) | `core/blocks/ts_analyzer.py` | `tests/blocks/test_ts_analyzer.py::test_every_program_map_pid_is_checked` |
| 1.6 PID(영상·소리 5 s, 자막·데이터 제외) | TR 101 290 V1.4.1(수치 같음) | ✅ | `core/blocks/ts_analyzer.py` | `tests/blocks/test_ts_analyzer.py::test_pid_limit_is_5_s_for_video_and_audio_only` |
| 여러 패킷에 걸친 PSI 구간 모으기, CRC-32/MPEG-2 | ISO 13818-1 2.4.4·부록 A | ✅ | `core/blocks/ts_analyzer.py` | `tests/blocks/test_ts_analyzer.py::test_sections_over_two_packets_are_read` |
| 2.1 TEI·2.2 CRC(PAT·PMT) 세기만(우선순위 1 합계·판정 제외) | TR 101 290 V1.4.1 5.2.2 | ✅ (2026-09-25) | `core/blocks/ts_analyzer.py` | `tests/blocks/test_ts_analyzer.py::test_bad_crc_pat_is_counted_and_is_no_pat` |
| 2.x PCR 반복·정확도, PTS 오류 | TR 101 290 P2, 05 4.6.4 | ⬜ P4(원인 분석) — 송신 파일은 실측 PCR ≤ 21 ms | | |
| 준비 파일 PAT ≤ 100 ms, PCR ≤ 40 ms | TR 101 154, iptv 3장 | ✅ 실측(iptv 7 S5) / 자동 검사 ⬜(준비 파일 검사 4.3) | `modules/iptv/sender/commands.py` | `tests/iptv/test_sender.py::test_prepare_command_per_quality` |

## 5. 수신 측 오염 방지
| 요구 | 근거 | 상태 | 코드 | 시험 |
|------|------|------|------|------|
| 분석·재생 소켓 분리, 중계 금지 | 05 4.2 | ✅ 분석 소켓 + 재생기(VLC)가 자기 소켓으로 가입(`--miface`), 중계 없음 | `core/blocks/udp_receiver.py`, `modules/iptv/receiver/player.py` | `tests/iptv/test_player.py::test_real_vlc_plays_restarts_and_stops` |
| 재생 여부는 재생기 자체 보고로만(RCV-019/020), 재생기가 죽어도 계측 계속(RCV-003) | 05 4.6, 09 RCV-003/019/020, iptv 3.2·3.6 | ✅ P2 VLC(scene 프레임 번호) / 표식·mpv는 P3 | `modules/iptv/receiver/player.py`, `modules/iptv/receiver/session.py` | `tests/iptv/test_run_controller.py::test_killed_player_does_not_touch_the_measurement` |
| 공유기 없는 기준선 시험(S-54)으로 수신 장비·재생기 조합 검증(손실 0 + 재생 이상 0), 모든 보고서에 마지막 기준선, 공유기가 사이에 있으면 NET-020 | 05 4.6.3, 10 1.4 | ✅ 장치·흐름 / 24시간 실측 ⬜(장비) | `controller/run.py`, `controller/baselines.py`, `modules/iptv/run.py` | `tests/controller/test_baseline.py::test_a_baseline_run_verifies_the_receiver_and_later_reports_show_it`, `tests/controller/test_baseline.py::test_a_router_in_between_refuses_the_baseline` |
| 수신 스레드는 받기만, 계산은 1초 단위 | 05 4.2 | ✅ | `modules/iptv/receiver/session.py` | `tests/iptv/test_receiver_session.py::test_full_cycle_counts_injected_loss` |
| 수신 버퍼 16 MB 요청·적용값 기록 | 05 4.3, RCV-001 | ✅ | `core/blocks/udp_receiver.py` | `tests/blocks/test_udp_receiver.py::test_receive_buffer_applied_value_is_reported` |
| 자체 드롭을 네트워크 손실과 분리 | 05 4.3, RCV-002 | ✅ | `core/blocks/udp_receiver.py`, `modules/iptv/cell_rules.py` | `tests/blocks/test_color_judge.py::test_network_loss_is_split_from_receiver_loss` |
| 시스템 전체 `UdpRcvbufErrors` | 05 4.3 | ⬜ | | |

## 6. 경계·판정
| 요구 | 근거 | 상태 | 코드 | 시험 |
|------|------|------|------|------|
| 안정화(첫 패킷·PAT·3초 깨끗·첫 화면), 최대 15 s | 05 2.1-2 | ✅ | `core/blocks/warmup_gate.py`, `modules/iptv/warmup.py` | `tests/blocks/test_warmup_gate.py::test_random_seconds_match_reference` |
| 중간 가입(첫 순번·첫 CC·늦은 PAT) 오류 아님 | 05 2.1-2, RUN-010 | ✅ | `modules/iptv/receiver/analyzer.py` | `tests/iptv/test_analyzer.py::test_mid_stream_join_is_not_an_error` |
| 안정화 뒤 첫 손실은 반드시 셈 | 05 2.1-2 규칙 7 | ✅ | `modules/iptv/receiver/analyzer.py` | `tests/iptv/test_analyzer.py::test_first_loss_after_warmup_is_caught` |
| 1초 집계 → 5분 요약, 채널 전환 시 구간 분리 | 05 5.1·5.2, 02 6 | ✅ | `core/blocks/summarizer.py` | `tests/blocks/test_summarizer.py::test_channel_change_splits_the_cell` |
| 5분 칸 색(초록·노랑·빨강·보라·회색) | 05 5.3 | ✅ | `modules/iptv/cell_rules.py` | `tests/blocks/test_color_judge.py::test_cell_colors` |
| 문제 구간으로 나누기(송신 문제 → 보라, 유선 수신 링크 끊김 → 회색, 구간 밖은 빨강 그대로) | 05 5.3·5.2, 09 SND-016·NET-015 | ✅ (실제 링크 끊김은 실기) | `modules/iptv/run.py::trouble_of`, `modules/iptv/run.py::cell_trouble`, `modules/iptv/cell_rules.py`, `controller/run.py::_note_trouble`, `controller/run.py::_judge_pending` | `tests/iptv/test_trouble_windows.py::test_monkey_never_hides_loss_outside_windows`, `tests/iptv/test_run_controller.py::test_a_killed_sender_is_the_tools_fault_not_the_routers` |
| 최종 판정 PASS/FAIL/INCOMPLETE | 05 5.4 | ✅ | `core/blocks/color_judge.py` | `tests/blocks/test_color_judge.py::test_final_verdict` |
| 최소 1,000 패킷(수신기별 전체·꽉 찬 칸), 부분 칸은 100 kbps | 05 2.1 | ✅ 칸 규칙 + 시험 전체 | `modules/iptv/cell_rules.py`, `modules/iptv/run.py` | `tests/iptv/test_run_rules.py::test_receiver_minimum_for_the_whole_run` |

## 7. 2026-09-24 보강한 표준 절 (05 1.8~1.11)에서 나온 할 일
| 요구 | 근거 | 상태 | 코드 | 시험 |
|------|------|------|------|------|
| 송신·유선 수신 NIC 링크 속도 < 필요 대역 × 1.1이면 시작 차단(NET-012) | 05 1.8 | ✅ (반이중은 경고) | `controller/links.py`, `modules/iptv/run.py` | `tests/iptv/test_run_controller.py::test_slow_link_blocks_the_start_net_012` |
| 시험 중 링크가 필요 대역 미만(NET-013)·기준 속도 미만(NET-014)이 되면 한 번 알림, 판정은 그대로 | 05 1.8 | ◐ 판단·사건·화면 ✅ / Problems 창·Action Required(F21·F22)·보고서 링크 이력 ⬜ | `controller/links.py` | `tests/iptv/test_run_controller.py::test_link_drop_during_the_test_net_013` |
| 링크 속도·이중·EEE·PAUSE 설정을 보고서 "시험 환경"에 기록 | 05 1.8 | ⬜ P3 | | |
| 태블릿 Wi-Fi 저지연 Lock + MulticastLock | 05 1.9·4.5 | ⬜ 태블릿 앱(P3) | | |
| 도착 지연 변동 peak-to-peak 40 ms 초과 = 노랑 | 05 1.10·5.3, TS 102 034 7.2.1.1 | ✅ | `modules/iptv/receiver/analyzer.py`, `modules/iptv/cell_rules.py` | `tests/iptv/test_analyzer.py::test_delay_range_follows_injected_delay` |
| 시계 기울기(skew) 추정·제거(가입 구간 최소 제곱, 120 s 이상, 200 ppm 넘으면 빼지 않음) | RFC 5481 7, RFC 3393 5.2, 05 2.2 | ✅ (2026-09-25, 두 PC 실측 확인은 새 PC 때) | `modules/iptv/receiver/analyzer.py` | `tests/iptv/test_analyzer.py::test_clock_skew_is_estimated_and_removed` |
| Join·Leave 500 ms 초과 횟수 기록·표시(판정 무관) | 03 F19, TS 102 034 7.2.2.2 | ◐ 시각 기록 ✅(전환 줄 `t_join`·`t_first_packet`·`t_leave`·`t_last_old_packet`) / 횟수 집계·표시(보고서 `channel_change`) ⬜ | `modules/iptv/receiver/zap.py` | `tests/iptv/test_zap.py::test_line_has_every_02_key_in_order` |
| IGMP 타이머 근거(GMI 260 s(v2)·270 s(v3)·125 s·Leave 2 s)로 원인 분석 | 05 1.11 | ⬜ P4 원인 분석(R-02·R-14) | | |
| heartbeat 왕복 시간을 장비별로 기록(φ accrual 2차 준비) | 12 4-4 | ⬜ Controller heartbeat 폴링(P2) | | |
| 패키지 형식(.zlm): 설명·해시 목록, 풀기 전에 전부 검사(변조·누락·추가·위험한 이름·링크·깨진 zip = INS-006), 같은 입력 같은 바이트 | 02 13, 06 원칙 8 | ✅ | `agent/deployer/package.py`, `tools/build.py` | `tests/agent/test_package.py::test_real_module_package_opens`, `tests/agent/test_package.py::test_same_input_same_bytes`, `tests/agent/test_package.py::test_changed_missing_extra_and_unsafe_files`, `tests/agent/test_package.py::test_a_link_in_the_zip_is_refused`, `tests/agent/test_package.py::test_random_byte_flips_never_pass` |
| 모듈 설치·제거: 설치 목록, 정확한 이름만 거꾸로, 설정 남김·`--purge`, 캐시·`__pycache__` 통째, 복구·업그레이드·다운그레이드 거부(INS-026), 모르는 파일 안 지움(INS-021), 실패·중단 되돌림(INS-013), 제거·재설치 10회 | 02 14, 06 12, 10 4-1 | ✅ | `agent/deployer/manifest.py`, `agent/deployer/modules.py` | `tests/agent/test_modules_install.py::test_install_then_remove_leaves_what_was_there`, `tests/agent/test_modules_install.py::test_settings_survive_removal_and_come_back`, `tests/agent/test_modules_install.py::test_dry_run_lists_and_changes_nothing`, `tests/agent/test_modules_install.py::test_repair_upgrade_and_downgrade`, `tests/agent/test_modules_install.py::test_refusals`, `tests/agent/test_modules_install.py::test_foreign_file_is_never_deleted`, `tests/agent/test_modules_install.py::test_other_module_untouched_over_ten_rounds`, `tests/agent/test_modules_install.py::test_failure_in_any_step_rolls_back`, `tests/agent/test_modules_install.py::test_crash_mid_install_is_rolled_back_at_next_start`, `tests/agent/test_modules_install.py::test_crash_mid_remove_is_finished_at_next_start`, `tests/agent/test_modules_install.py::test_monkey_install_remove` |
| Deployer 10991: health·받기(해시)·설치·목록·제거, 같은 command_id 한 번, 한 번에 하나(INS-027), 시험 중 거부(INS-022), 노드 10초 무응답 → 재시작 + 쪽지(SYS-009), L0는 Core 코드 안 씀 | 02 15, 06 11.2, 09 SYS-009, 12 1-1 | ✅ | `agent/deployer/server.py`, `agent/deployer/agent_link.py`, `agent/deployer/__main__.py`, `core/node.py` | `tests/agent/test_deployer_service.py::test_health_matches_02`, `tests/agent/test_deployer_service.py::test_receive_install_list_remove`, `tests/agent/test_deployer_service.py::test_package_json_matches_02`, `tests/agent/test_deployer_service.py::test_same_command_id_runs_once`, `tests/agent/test_deployer_service.py::test_refusals`, `tests/agent/test_deployer_service.py::test_silent_node_is_restarted_with_a_note`, `tests/agent/test_deployer_service.py::test_no_restart_while_installing`, `tests/agent/test_deployer_e2e.py::test_module_goes_in_and_out_of_a_running_node`, `tests/agent/test_deployer_e2e.py::test_hung_node_is_restarted_and_says_so`, `tests/test_layering.py::test_deployer_uses_no_core_code` |
| Controller 배포 창구·CLI: `deploy`, `modules list/remove`(제거 `--yes`), 설치 중 시험 시작 대기·시험 중 거부, Deployer 무응답은 1·2·4 s 재시도 뒤 INS-014(종료 5), 장비 목록 `deployer_port`, 설치된 모듈 폴더에서 불러오기 | 02 2-5·11-0, 13, 12 1-3 | ✅ | `controller/deploy.py`, `controller/service.py`, `controller/cli.py`, `controller/devices.py`, `controller/run.py`, `core/protocol/client.py`, `core/modules/registry.py` | `tests/agent/test_deployer_e2e.py::test_cli_deploy_list_remove`, `tests/agent/test_deployer_e2e.py::test_a_deployer_that_comes_up_late_is_retried` |
| Linux .deb: 노드·Deployer 실행 파일 따로, 유닛 두 개(zeroloss 계정·권한·control-group), conffile은 zeroloss.conf만, 모듈은 넣지 않음, 모듈이 쓰는 표준 라이브러리는 넣음, 얼린 노드가 바깥 모듈을 불러 송출·수신 | 06 4.3·4.3-1·11.3 | ✅(실제 PC 설치는 장비 올 때) | `tools/build.py`, `tools/entry/node.py`, `tools/entry/deployer.py`, `installer/linux/postinst`, `installer/linux/prerm`, `installer/linux/postrm`, `installer/linux/zeroloss-agent.service`, `installer/linux/zeroloss-deployer.service` | `tests/agent/test_build_deb.py::test_package_contents`, `tests/agent/test_build_deb.py::test_frozen_node_loads_and_runs_a_module`, `tests/agent/test_build_deb.py::test_frozen_deployer_answers` |
| 설치 스크립트: 계정(원래 있으면 안 지움)·폴더·sysctl 원래 값·ufw(켜져 있을 때만)·노드 설치 목록, 업그레이드는 멈춤만, 제거는 모듈 코드·방화벽·sysctl 되돌림, purge 뒤 설치 전과 같음 | 06 4.3-1, 06 9 | ✅ | `agent/deployer/nodesetup.py`, `agent/deployer/__main__.py` | `tests/agent/test_nodesetup.py::test_setup_makes_what_the_table_says`, `tests/agent/test_nodesetup.py::test_setup_again_is_an_upgrade`, `tests/agent/test_nodesetup.py::test_upgrade_prerm_only_stops`, `tests/agent/test_nodesetup.py::test_install_remove_purge_is_as_before`, `tests/agent/test_nodesetup.py::test_no_firewall_when_ufw_is_off`, `tests/agent/test_nodesetup.py::test_existing_account_is_kept`, `tests/agent/test_nodesetup.py::test_scripts_call_the_right_step`, `tests/agent/test_nodesetup.py::test_postinst_ignores_other_actions`, `tests/agent/test_nodesetup.py::test_postrm_purge_removes_data_and_only_our_account`, `tests/agent/test_nodesetup.py::test_postrm_remove_keeps_data`, `tests/agent/test_nodesetup.py::test_units_are_valid` |
| SSH 최초 설치: 인증 전에 호스트 키(처음엔 저장·바뀌면 INS-009), 로그인(INS-007/008), 점검(INS-005/002/003), sudo(INS-010), 잠금 대기(INS-011), dpkg 실패 되돌림(INS-028), 응답 확인(INS-014), 장비 등록·모듈 전달, 도구 확인, 비밀번호 안 남김, CLI `--yes` | 06 11.3, 13 | ✅(실제 PC는 장비 올 때) | `controller/ssh_install.py`, `controller/service.py`, `controller/cli.py` | `tests/controller/test_ssh_install.py::test_install_end_to_end`, `tests/controller/test_ssh_install.py::test_host_key_same_changed_and_accepted`, `tests/controller/test_ssh_install.py::test_login_and_sudo_refusals`, `tests/controller/test_ssh_install.py::test_lock_is_waited_for`, `tests/controller/test_ssh_install.py::test_lock_that_never_ends`, `tests/controller/test_ssh_install.py::test_dpkg_failure_is_rolled_back`, `tests/controller/test_ssh_install.py::test_checks_before_anything_is_sent`, `tests/controller/test_ssh_install.py::test_node_that_never_answers`, `tests/controller/test_ssh_install.py::test_unreachable_pc`, `tests/controller/test_ssh_install.py::test_cli_install_linux` |
