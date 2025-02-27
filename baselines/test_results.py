import numpy as np
import pandas as pd
import os
import json

# The file all_eval_runs are generated by the wandb_data.py
df_eval = pd.read_csv('all_eval_runs.csv')
df_test_results = df_eval[["run","group"]].drop_duplicates()

all_runs = df_test_results["run"].to_list()

colnames = ["run","percentage_complete_mean","normalized_reward_mean"]
df_test_metrics = pd.DataFrame(columns= colnames)
for cur_run in all_runs:
    result_file = "checkpoints/"+ cur_run + "/test_outcome.json"
    if os.path.isfile(result_file):
        with open(result_file) as f:
            data = json.load(f)
        df_test_metrics = df_test_metrics.append({colnames[0]:cur_run,colnames[1]:data.get(colnames[1]),colnames[2]:data.get(colnames[2])},ignore_index = True)
        
df_test = pd.merge(df_test_metrics,df_test_results,how='left')

df_all_final_results = df_test.groupby("group").aggregate([np.mean,np.std]).reset_index()

df_all_final_results.to_csv('test_results_group.csv',index=False)
