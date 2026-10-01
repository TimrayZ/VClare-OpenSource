import os
import re
import shlex
import shutil
import subprocess
import json
import csv
import signal


def clean_folder_except(folder_path):
    """
    删除文件夹中除了testbench.sv和verilog.sv之外的所有文件和文件夹
    
    参数:
    folder_path (str): 要清理的文件夹路径
    """
    # 检查文件夹是否存在
    if not os.path.exists(folder_path):
        print(f"错误: 文件夹 '{folder_path}' 不存在")
        return
    
    # 要保留的文件列表
    keep_files = {'testbench.sv', 'verilog.sv'}
    
    try:
        # 遍历文件夹中的所有内容
        for item in os.listdir(folder_path):
            item_path = os.path.join(folder_path, item)
            
            # 如果要保留的文件，跳过
            if item in keep_files:
                print(f"保留文件: {item}")
                continue
            
            try:
                # 删除文件
                if os.path.isfile(item_path):
                    os.remove(item_path)
                    print(f"已删除文件: {item}")
                
                # 删除文件夹
                elif os.path.isdir(item_path):
                    shutil.rmtree(item_path)
                    print(f"已删除文件夹: {item}")
                    
            except Exception as e:
                print(f"删除 {item} 时出错: {e}")
                
        print("清理完成！")
        
    except Exception as e:
        print(f"遍历文件夹时出错: {e}")

def run_iverilog(problem_name, case_idx, base_path):
    """
    使用 Iverilog 编译并运行仿真（多顶层候选自适应）。
    返回值：1=编译失败；2=仿真失败/超时；3=仿真成功
    """

    work_path = os.path.join(base_path, problem_name)
    tb_path   = os.path.join(work_path, "testbench.sv")
    dut_path  = os.path.join(work_path, f"case{case_idx}.sv")
    vvp_path  = os.path.join(work_path, "test.vvp")

    if not os.path.isfile(tb_path) or not os.path.isfile(dut_path):
        print(f"找不到源码：tb={os.path.isfile(tb_path)} dut={os.path.isfile(dut_path)}")
        # clean_folder_except(work_path)
        return 1

    # 逐个尝试编译
    build_logs = []

    tb_q, dut_q, vvp_q = shlex.quote(tb_path), shlex.quote(dut_path), shlex.quote(vvp_path)
    cmd = (
        f'bash -lc "iverilog -g2012 '
        f'-o {vvp_q} '
        f'{tb_q} {dut_q}"'
    )
    
    print(f"\n=== problem : {problem_name}, case_idx: {case_idx} ===")
    p = subprocess.Popen(cmd, shell=True, cwd=work_path,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = p.communicate()
    log = (out + err).decode(errors="ignore")
    print(log)
    build_logs.append((p.returncode, log))

    if p.returncode == 0:
        print(f"编译成功")
        sim_path = vvp_path
        if not (os.path.isfile(sim_path) and os.access(sim_path, os.X_OK)):
            if os.path.isfile(sim_path):
                try:
                    os.chmod(sim_path, 0o755)
                except Exception:
                    pass
        if not (os.path.isfile(sim_path) and os.access(sim_path, os.X_OK)):
            print("未找到仿真可执行文件（预期在 {}/test.vvp）".format(work_path))
            clean_folder_except(work_path)
            return 1

        # 运行仿真
        try:
            cmd = (
                f'bash -lc "vvp '
                f'{vvp_q} '
            )
            sim = subprocess.Popen(shlex.quote(sim_path), shell=True, cwd=work_path,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            so, se = sim.communicate(timeout=20)
            output = (so + se).decode(errors="ignore")
            print(output)
            rc = sim.returncode

            # 杀死所有运行的vvp进程
            try:
                os.kill(sim.pid, signal.SIGTERM)  # 发送终止信号
                print(f"Terminated process with PID {sim.pid}")
            except ProcessLookupError:
                print(f"Process with PID {sim.pid} not found. It may have already exited.")            

            success_marks = [
                "Hint: Total mismatched samples is 0 out of",
                "Your Design Passed",
                "Mismatches: 0 in",
                "ALL TESTS PASSED",
            ]
            fail_hints = [
                "Your Design Failed", "Assertion failed",
                "ERROR", "Error", "%Error"
            ]
            #if any(m in output for m in success_marks) or (rc == 0 and not any(h in output for h in fail_hints)):
            if any(m in output for m in success_marks):
                print("√√√√√√√√√√√仿真成功√√√√√√√√√√√√")
                # clean_folder_except(work_path)
                return 3
            print("×××××××××××仿真失败××××××××××××")
            # clean_folder_except(work_path)
            return 2

        except subprocess.TimeoutExpired:
            sim.kill()
            print("仿真超时，跳过该次仿真")
            # clean_folder_except(work_path)
            return 2
        except Exception as e:
            print(f"执行仿真时发生错误: {e}")
            # clean_folder_except(work_path)
            return 2
    else:
        print(f"编译失败")
        return 1

def judge_task(task_name, tb_code, cases_dict, base_path, timeout=20, truth_labels=None):
    """
    为单个 task 写入 testbench 和 case 文件，运行 run_iverilog 判定每个 case。
    总是返回 (results, metrics)：
      - results: 长度为5的 "pass"/"fail" 列表
      - metrics: 如果传入 truth_labels（长度为5 或可规范化为5）则返回指标字典，否则返回 None
    score.py 内部会在有 truth_labels 时打印 metrics 到命令行；main.py 不需要关心 metrics。
    """
    abs_base = os.path.abspath(base_path)
    task_path = os.path.join(abs_base, task_name)
    os.makedirs(task_path, exist_ok=True)

    tb_path = os.path.join(task_path, "testbench.sv")
    try:
        with open(tb_path, 'w', encoding='utf-8') as f:
            f.write(tb_code or "")
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
    except Exception as e:
        print(f"写入 testbench 失败: {e}")
        # 如果没有 truth_labels，metrics 为 None；否则返回零值 metrics 以便上层处理
        metrics = None
        if truth_labels is not None:
            metrics = {"true_positive": 0, "true_negative": 0, "false_positive": 0, "false_negative": 0,
                       "accuracy": 0.0, "precision": 0.0, "recall": 0.0, "f1": 0.0}
        return ["fail"] * 5, metrics

    # discover available cases in cases_dict (case1, case2, ...), sort numerically
    case_keys = [k for k in cases_dict.keys() if isinstance(k, str) and k.startswith("case")]
    def _case_index(k):
        try:
            return int(k[4:])
        except Exception:
            return 0

    case_keys = sorted(case_keys, key=_case_index)
    case_values = [cases_dict.get(k, "") or "" for k in case_keys]

    # if no case keys found, try fallback: treat as empty single case
    if not case_values:
        case_values = [""]

    results = []
    # write normalized case files named case1..caseN
    for j, content in enumerate(case_values, start=1):
        verilog_path = os.path.join(task_path, f"case{j}.sv")
        try:
            with open(verilog_path, 'w', encoding='utf-8') as f:
                f.write(content)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass
        except Exception as e:
            print(f"写入 {verilog_path} 失败: {e}")
            results.append("fail")
            continue

        # debug: 确认文件存在
        if not os.path.isfile(tb_path):
            print(f"错误: testbench 文件不存在: {tb_path}")
            results.append("fail")
            continue
        if not os.path.isfile(verilog_path):
            print(f"错误: dut 文件不存在: {verilog_path}")
            results.append("fail")
            continue

        try:
            res = run_iverilog(task_name, j, abs_base)
        except Exception as e:
            print(f"run_iverilog 调用异常: {e}")
            res = 2

        if res == 3:
            results.append("pass")
        else:
            results.append("fail")

    # 如果没有提供真值标签，返回 metrics 为 None（兼容 main 不处理 metrics 的方式）
    if truth_labels is None:
        return results, None

    # 规范化 truth_labels（接受 "pass"/"fail" 或 True/False）
    def norm_label(x):
        if isinstance(x, str):
            return "pass" if x.lower() == "pass" else "fail"
        return "pass" if bool(x) else "fail"

    truths = [norm_label(x) for x in truth_labels]
    if len(truths) != len(results):
        print(f"truth_labels 长度 ({len(truths)}) 与结果长度 ({len(results)}) 不一致，忽略指标计算")
        return results, None

    tp = fp = tn = fn = 0
    for pred, truth in zip(results, truths):
        if pred == "pass" and truth == "pass":
            tp += 1
        elif pred == "pass" and truth == "fail":
            fp += 1
        elif pred == "fail" and truth == "fail":
            tn += 1
        elif pred == "fail" and truth == "pass":
            fn += 1

    total = tp + tn + fp + fn
    accuracy = (tp + tn) / total if total > 0 else 0.0
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0

    metrics = {
        "true_positive": tp,
        "true_negative": tn,
        "false_positive": fp,
        "false_negative": fn,
        "accuracy": accuracy,
        "precision": precision,
        "recall": recall,
        "f1": f1
    }

    # 仅在 score.py 中打印指标，main.py 不需要再打印
    print(f"Metrics for {task_name}: accuracy={accuracy:.4f}, precision={precision:.4f}, recall={recall:.4f}, f1={f1:.4f}")
    print(f"  TP={tp} TN={tn} FP={fp} FN={fn}")

    return results, metrics



if __name__ == "__main__":

    DATA = 'public'
    gen_tb_path = f"/data/yzhou/verilogGen/idea_tb_gen/full_test_cross_validation/saves/{DATA}_save.json"
    case_path = f"/data/yzhou/verilogGen/idea_tb_gen/full_test_cross_validation/data/{DATA}_data.json"
    truth_table_path = f"/data/yzhou/verilogGen/idea_tb_gen/full_test_cross_validation/data/{DATA}_data_truth_table.csv"
    base_path = f"/data/yzhou/verilogGen/idea_tb_gen/full_test_cross_validation/saves/run_iverilog"

    success_case = 0
    os.makedirs(base_path, exist_ok=True)

    with open(truth_table_path, newline="", encoding="utf-8") as f_t:
        reader = csv.reader(f_t)
        rows = [row for row in reader]
    truth_table = [[]] + rows

    testbench_all = {}
    with open(gen_tb_path, newline="", encoding="utf-8") as f_g:
        tb_j = json.load(f_g)
        for item in tb_j:
            testbench = {}
            # testbench["verilog_tb"] = item["verilog_tb"]
            # testbench["python_tb"] = item["python_tb"]
            testbench["tb"] = item["tb"]
            testbench_all[item["name"]] = testbench
    
    syntax_error = 0
    true_positive = 0
    true_negative = 0
    false_positive = 0
    false_negative = 0
    goldenref_pass = 0

    fail_name = []
    fail_idx = []
    with open(case_path,'r',encoding = 'utf-8') as f_c:
        data = json.load(f_c)
        total_case = 5 * len(data)
        for i in range(len(data)):

            task_id = data[i]["name"]
            task_path = os.path.join(base_path, task_id)
            testbench_path = os.path.join(task_path, "testbench.sv")
            # testbench_pypath = os.path.join(task_path, "testbench.py")

            os.makedirs(task_path, exist_ok=True)
            if task_id in testbench_all:
                with open(testbench_path,'w', encoding='utf-8') as f:
                    f.write(str(testbench_all[task_id]["tb"]))
                # with open(testbench_path,'w', encoding='utf-8') as f:
                #     f.write(str(testbench_all[task_id]["verilog_tb"]))
                # with open(testbench_pypath,'w', encoding='utf-8') as f:
                #     f.write(str(testbench_all[task_id]["python_tb"]))
            else:
                syntax_error += 5
                continue

            for j in range(1,6):
                verilog_path = os.path.join(task_path, f"case{j}.sv")
                with open(verilog_path,'w', encoding='utf-8') as f:
                    f.write(data[i][f"case{j}"])
                result = run_iverilog(task_id, j, base_path)
                if ((result == 3 and truth_table[i+1][j] == "pass") or (result == 2 and truth_table[i+1][j] == "fail")):
                    success_case = success_case + 1
                    
                    if (result == 3 and truth_table[i+1][j] == "pass"):
                        true_positive += 1
                        
                    elif (result == 2 and truth_table[i+1][j] == "fail"):
                        true_negative += 1

                    if j == 1: 
                        goldenref_pass += 1
                elif (result == 3 and truth_table[i+1][j] == "fail"):
                    false_positive += 1
                    fail_name.append(task_id)
                    fail_idx.append(j)
                elif (result == 2 and truth_table[i+1][j] == "pass"):
                    false_negative += 1
                    fail_name.append(task_id)
                    fail_idx.append(j)
                elif result == 1:
                    syntax_error += 1
                    if truth_table[i+1][j] == "fail":
                        false_positive += 1
                    if truth_table[i+1][j] == "pass":
                        false_negative += 1
                        
    precision = true_positive / (true_positive + false_positive)
    recall = true_positive / (true_positive + false_negative)

    print("Eval0 = ", 1-syntax_error/total_case)
    print("Eval1 = ", goldenref_pass/len(data))
    print("success rate is(Eval2): ", success_case/total_case)  
    print("syntax_error rate is: ", syntax_error/total_case)  
    print(f"true positive = {true_positive}")
    print(f"true negative = {true_negative}")
    print(f"false positive = {false_positive}")
    print(f"false negative = {false_negative}")
    print(f"accuracy = {(true_negative + true_positive)/(true_positive + true_negative + false_negative + false_positive)}")
    print(f"precision = {precision}")
    print(f"recall = {recall}")
    print(f"f1-score = {2 * precision * recall / (precision + recall)}")

    for fail_case in range(len(fail_name)):
        print(fail_name[fail_case], " ", fail_idx[fail_case])





