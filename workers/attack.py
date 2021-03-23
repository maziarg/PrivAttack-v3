import gc
import math
import os
import uuid
from random import randint, SystemRandom
import BCQutils
import BCQ
import pathlib

import numpy as np
import xgboost as xgb
from scipy.stats.mstats import gmean

from pandas import DataFrame
from utils.configs import CORRELATED, DECORRELATED, SEMI_CORRELATED, CORRELATION_MAP
from utils.helpers import cleanup, print_experiment, generate_pairs, get_models

# anonymous functions to randomly select a number of items in an np.array
RAND_SELEC_FUNC_REPLACE_FALSE = lambda data, num: np.random.choice(data, num, replace=False)
RAND_SELEC_FUNC_REPLACE_TRUE = lambda data, num: np.random.choice(data, num, replace=True)


def get_random_seqs(seq_source, seq_size, eval_size):
    # To randomly select train, test, and eval items, we need to cache train and test first,
    # then remove the selected ones from the entire list, and then select eval ones
    source = list(range(seq_source))
    seq_selected = RAND_SELEC_FUNC_REPLACE_FALSE(source, seq_size)
    # we need to remove the selected items before we select eval seq
    source = [val for val in source if val not in seq_selected]
    eval_seq_selected = RAND_SELEC_FUNC_REPLACE_FALSE(source, eval_size)
    return seq_selected, eval_seq_selected


def get_trajectory(seed, index, trajectory_length):
    path = "tmp/"
    npy_train = path + str(seed) + '_' + str(trajectory_length) + '.npy'

    return np.load(npy_train, 'r', allow_pickle=True)[index]


def get_trajectory_test(seed, index, trajectory_length):
    path = "tmp/"
    npy_test = path + str(seed) + '_' + str(trajectory_length) + '_test.npy'

    return np.load(npy_test, 'r', allow_pickle=True)[index]


def compute_max_trajectory_length(trajectories_end_indices):
    """
    from the list of trajectory end indexes, finds the maximum trajectory length
    """
    max_length = 0
    # The following is because the first item in the for loop would be 1 less than the expected value
    previous_index = -1
    for index in trajectories_end_indices:
        max_length = max((index - previous_index), max_length)
        previous_index = index
    return max_length


def pad_traj(traj, padd_len):
    """adds padding to a trajectory"""
    if not isinstance(traj, np.ndarray):
        raise Exception("Failed to padd the trajectory: Wrong trajectory type")
    padding_element = np.asarray(traj[-1]).repeat(int(padd_len) - traj.size)
    test_seq = np.concatenate((traj, padding_element))
    return test_seq


def get_seeds_train_pairs(label, seeds):
    """
    To create trajectory pairs
    For label 1, need to pair train and test from seed 0
    For label 0, need to pair test from seed 0 and train from seed 1
    Note: evidence == test == seed 0
    """
    if label:
        train_seed = int(seeds[0])
        test_seed = int(seeds[0])
    else:
        train_seed = int(seeds[1])
        test_seed = int(seeds[0])

    return train_seed, test_seed


def get_seeds_test_pairs(label, seeds):
    """Following the logic from get_seeds_train_pairs"""
    if label:
        train_seed = int(seeds[2])
        test_seed = int(seeds[2])
    else:
        train_seed = int(seeds[3])
        test_seed = int(seeds[2])

    return train_seed, test_seed


def get_buffer_properties(buffer_name, attack_path, state_dim, action_dim, device, args, seed):
    """Loads buffers and returns some buffer properties"""
    print("Retreiving buffer properties...")
    replay_buffer_train = BCQutils.ReplayBuffer(state_dim, action_dim, device)
    replay_buffer_train.load(f"./{attack_path}/{seed}/{args.max_traj_len}/buffers/{buffer_name}")

    num_trajectories = replay_buffer_train.num_trajectories
    start_states = replay_buffer_train.initial_state
    trajectories_end_index = replay_buffer_train.trajectory_end_index

    return num_trajectories, start_states, trajectories_end_index


def create_pairs(
    attack_path, state_dim, action_dim, device, args, label, train_seed, test_seed,
    do_train=True, train_padding_len=0, test_padding_len=0):

    # Getting training buffer properties
    buffer_name_train = f"{args.buffer_name}_{args.env}_{train_seed}"
    train_num_trajectories, train_start_states, train_trajectories_end_index = get_buffer_properties(
        buffer_name_train, attack_path, state_dim, action_dim, device, args, train_seed)

    # BCQ output
    buffer_name_test = f"target_{args.buffer_name}_{args.env}_{test_seed}"
    test_num_trajectories, test_start_states, test_trajectories_end_index = get_buffer_properties(
        buffer_name_test , attack_path, state_dim, action_dim, device, args, test_seed)

    # Bounding the number of test trajectories
    if args.out_traj_size < test_num_trajectories:
        test_num_trajectories = args.out_traj_size

    if args.in_traj_size < train_num_trajectories:
        train_num_trajectories = args.in_traj_size

    # Choosing 80% of input trajectories for training and the rest of evaluation
    train_size = math.floor(train_num_trajectories * 0.80)
    eval_train_size = train_num_trajectories - train_size

    # Choosing 80% of output trajectories for training and the rest of evaluation
    test_size = math.floor(test_num_trajectories * 0.80)
    eval_test_size = test_num_trajectories - test_size

    # Loading test/train action buffers
    test_seq_buffer = np.ravel(np.load(
        f"./{attack_path}/{test_seed}/{args.max_traj_len}/buffers/{buffer_name_test}_action.npy"))
    train_seq_buffer = np.ravel(np.load(
        f"./{attack_path}/{train_seed}/{args.max_traj_len}/buffers/{buffer_name_train}_action.npy"))

    if CORRELATION_MAP.get(args.correlation) == DECORRELATED:
        return generate_decorrelated_train_eval_pairs(
                test_seq_buffer, train_seq_buffer, train_start_states, test_start_states,
                test_size, train_size, eval_test_size, eval_train_size, args.max_traj_len, label, do_train=do_train
        )
    else:
        return generate_correlated_train_eval_pairs(
                test_seq_buffer, train_seq_buffer, test_trajectories_end_index, train_trajectories_end_index,
                test_size, train_size, eval_test_size, eval_train_size, test_padding_len, train_padding_len,
                train_start_states, test_start_states, label, do_train=do_train, correlation=args.correlation
        )


def generate_correlated_train_eval_pairs(
    test_seq_buffer, train_seq_buffer, test_trajectories_end_index, train_trajectories_end_index, test_size, train_size,
    eval_test_size, eval_train_size, test_padding_len, train_padding_len, train_start_states, test_start_states,
    label, do_train=True, correlation=CORRELATED):
    """Generating correlated train/eval pairs. It randomly selects trajectories from test and train datasets and pairs them"""
    test_traj_indecies, test_eval_indicies = get_random_seqs(
        len(test_trajectories_end_index), test_size, eval_test_size)
    train_traj_indecies, train_eval_indicies = get_random_seqs(
        len(train_trajectories_end_index), train_size, eval_train_size)

    final_train_dataset = generate_correlated_pairs(
        test_seq_buffer, train_seq_buffer, test_trajectories_end_index, train_trajectories_end_index,
        test_traj_indecies, train_traj_indecies,
        test_padding_len, train_padding_len, train_start_states, test_start_states, label, correlation=correlation)

    # when creating test pairs, we don't have evaluation part
    final_eval_dataset = None
    if do_train:
        final_eval_dataset = generate_correlated_pairs(
            test_seq_buffer, train_seq_buffer, test_trajectories_end_index, train_trajectories_end_index,
            test_eval_indicies, train_eval_indicies,
            test_padding_len, train_padding_len, train_start_states, test_start_states, label, correlation=correlation)

    return final_train_dataset, final_eval_dataset

def generate_correlated_pairs(
    test_seq_buffer, train_seq_buffer, test_trajectories_end_index, train_trajectories_end_index,
    test_traj_indecies, train_traj_indecies,
    test_padding_len, train_padding_len, train_start_states, test_start_states, label, correlation=CORRELATED):
    """Pairing test and train pairs"""
    final_train_dataset = None
    final_train_dataset_label = None
    print(f"generating {CORRELATION_MAP.get(correlation)} pairs...")
    # Pairing the entire training with test in the broadcast fashion
    for j in test_traj_indecies:
        # Pairing the entire train set with the j-th test trajectory
        if j == 0:
            # from 0 to the end index inclusive
            test_seq = test_seq_buffer[0:test_trajectories_end_index[j] + 1: 1]
        else:
            # test_trajectories_end_index[j - 1] is part of the (j - 1)'s trajectory!
            test_seq = test_seq_buffer[test_trajectories_end_index[j - 1] + 1: test_trajectories_end_index[j] + 1: 1]
        # Padding test trajectories till the maximum length trajectory achieves
        # TODO: Note that the maximum trajectory length would not be padded! Would it confuse xgboost or other classifiers?
        # TODO: should we choose a good enough maximum length to which ALL trajectories would be padded?
        test_seq = pad_traj(test_seq, test_padding_len)
        for i in train_traj_indecies:
            # TODO seems like start states are of type ndarray, add checks if it was not the case later on.
            start_seq = np.concatenate((np.asarray(train_start_states[i]), np.asarray(test_start_states[j])))
            if i == 0:
                # from 0 to end index inclusive
                train_seq = train_seq_buffer[0:train_trajectories_end_index[i] + 1: 1]
            else:
                # from i - 1 to i inclusive
                train_seq = train_seq_buffer[train_trajectories_end_index[i - 1] + 1: train_trajectories_end_index[i] + 1: 1]
            # Padding train trajectories
            train_seq = pad_traj(train_seq, train_padding_len)
            # Putting start seq, train and test trajectories together
            # For Semi correlated pairs, we shuffle train and test trajectories in place
            if CORRELATION_MAP.get(correlation) == SEMI_CORRELATED:
                np.random.shuffle(train_seq)
                np.random.shuffle(test_seq)
            complete_traj_seq = np.concatenate((start_seq, train_seq, test_seq))
            # saving labels as a separate ndarray
            final_train_dataset_label = np.array([label]) if not isinstance(
                final_train_dataset_label, np.ndarray) else np.vstack((final_train_dataset_label, np.array([label])))

            # TODO: for now, we are both saving the arrays in a file on disk. This is in parallel with returning the result
            # TODO: After measuring the performance, just use one of the methods!
            # with open(f"./{attack_path}/{seed}/attack_outputs/traj_based_buffers/train_{args.out_traj_size}.npy", 'ab')\
            #         as f:
            #     # Concatenating the label to the trajectories here since this is a parallel transfer of data,
            #     # then save the file
            #     np.save(f, np.concatenate((complete_traj_seq, np.array([label]))))

            # vertically stack the trajectories to be fed into xgboost or anothe classifier
            final_train_dataset = complete_traj_seq if not isinstance(
                final_train_dataset, np.ndarray) else np.vstack((final_train_dataset, complete_traj_seq))

    print(f"generating correlated pairs...DONE!")
    # we return a tuple of trajectories and lables. XGBoost needs a matrix of data and label
    return (final_train_dataset, final_train_dataset_label)


def generate_decorrelated_train_eval_pairs(
    test_seq_buffer, train_seq_buffer, train_start_states, test_start_states,
    test_size, train_size, eval_test_size, eval_train_size, max_traj_len, label, do_train=True):
    """Generating decorrelated train/eval pairs"""
    final_train_dataset = generate_decorrelated_pairs(
        test_seq_buffer, train_seq_buffer, train_start_states, test_start_states,
        test_size, train_size, max_traj_len, label)

    final_eval_dataset = None
    if do_train:
        final_eval_dataset = generate_decorrelated_pairs(
            test_seq_buffer, train_seq_buffer, train_start_states, test_start_states,
            eval_test_size, eval_train_size, max_traj_len, label)
    return final_train_dataset, final_eval_dataset


def generate_decorrelated_pairs(
    test_seq_buffer, train_seq_buffer, train_start_states, test_start_states,
    test_size, train_size, max_traj_len, label
):
    """
    Randomly selects start states, action train/test_seq_buffer, and label
    A trajectory length is set using args.max_traj_len. This value should be the length of the entire
    trajectory.
    """
    final_train_dataset = None
    final_train_dataset_label = None
    # The construction of a trajectory and its maximum length is as follows:
    # args.max_traj_len = 1 (start train state) + 1 (start test state) + train_seq + test_seq
    # So, the size of train_seq and tes_seq is (args.max_traj_len - 2) // 2
    # Note: If args.max_traj_len is an odd value, the resultant max_traj_len is arg.max_traj_len - 1
    traj_len = (max_traj_len - 2) // 2
    print(f"generating decorrelated pairs...")
    for j in range(test_size):
        # Test seq
        test_seq = RAND_SELEC_FUNC_REPLACE_TRUE(test_seq_buffer, traj_len)
        for i in range(train_size):
            # Start seq, randomly selecting one start state for each test and train
            start_seq = np.concatenate(
                (
                    np.asarray(train_start_states[np.asscalar(RAND_SELEC_FUNC_REPLACE_TRUE(range(train_size), 1))]),
                    np.asarray(test_start_states[np.asscalar(RAND_SELEC_FUNC_REPLACE_TRUE(range(test_size), 1))])
                ))
            # Train seq
            train_seq = RAND_SELEC_FUNC_REPLACE_TRUE(train_seq_buffer, traj_len)
            # Putting start seq, train and test trajectories together
            complete_traj_seq = np.concatenate((start_seq, train_seq, test_seq))
            # saving labels as a separate ndarray
            final_train_dataset_label = np.array([label]) if not isinstance(
                final_train_dataset_label, np.ndarray) else np.vstack((final_train_dataset_label, np.array([label])))

            # vertically stack the trajectories to be fed into xgboost or anothe classifier
            final_train_dataset = complete_traj_seq if not isinstance(
                final_train_dataset, np.ndarray) else np.vstack((final_train_dataset, complete_traj_seq))

    print(f"generating decorrelated pairs...DONE!")
    # we return a tuple of trajectories and lables. XGBoost needs a matrix of data and label
    return (final_train_dataset, final_train_dataset_label)


def create_sets(seeds, attack_training_size, timesteps, trajectory_length, num_predictions, dimension):
    path = "tmp_plks/"
    if not os.path.exists(path):
        os.mkdir(path)

    train_size = math.floor(attack_training_size / (10 / 8))
    eval_size = math.floor(attack_training_size / (10 / 2))
    num_traj_per_model = int(timesteps / trajectory_length)
    total_pairs_needed = attack_training_size + num_predictions
    data_length = 2 * (trajectory_length * dimension)

    data_train = np.empty([0, data_length])
    data_eval = np.empty([0, data_length])
    labels_train = np.empty(train_size, dtype=int)
    labels_eval = np.empty(eval_size, dtype=int)
    data_test = np.empty([0, data_length])
    labels_test = []
    # train and test pair inedcies
    train_pairs, test_pairs = generate_pairs(total_pairs_needed, len(seeds) * num_traj_per_model, num_predictions,
                                             attack_training_size)

    d_test = str(uuid.uuid4())
    d_t = str(uuid.uuid4())
    l_t = str(uuid.uuid4())
    d_e = str(uuid.uuid4())
    l_e = str(uuid.uuid4())

    # save test pairs
    indx = 0
    for x_i, y_i in test_pairs:
        x_model, y_model, same_set, index_x, index_y = get_models(x_i, y_i, num_traj_per_model)
        seed_x = seeds[x_model]
        seed_y = seeds[y_model]
        data_test = np.insert(data_test, indx,
                              np.concatenate((get_trajectory(seed_x, index_x, trajectory_length),
                                              get_trajectory_test(seed_y, index_y, trajectory_length))), axis=0)

        if same_set:
            labels_test.append(1)
        else:
            labels_test.append(0)

        indx += 1

    np.save(path + d_test + '.npy', data_test)

    del test_pairs, data_test
    gc.collect()
    print("saved test pairs")

    # save train pairs
    indx = 0
    for x_i, y_i in train_pairs:
        if indx < train_size:
            x_model, y_model, same_set, index_x, index_y = get_models(x_i, y_i, num_traj_per_model)
            seed_x = seeds[x_model - 1]
            seed_y = seeds[y_model - 1]
            data_train = np.insert(data_train, indx, np.concatenate(
                (get_trajectory(seed_x, index_x, trajectory_length),
                 get_trajectory_test(seed_y, index_y, trajectory_length))),
                                   axis=0)

            if same_set:
                labels_train.put(indx, 1)
            else:
                labels_train.put(indx, 0)

        else:
            break

        indx += 1

    np.save(path + d_t + '.npy', data_train)
    np.save(path + l_t + '.npy', labels_train)

    del data_train, labels_train
    gc.collect()
    print("saved train pairs")

    # save eval pairs
    insrt = 0
    while indx < attack_training_size:
        x_i, y_i = train_pairs[indx]
        x_model, y_model, same_set, index_x, index_y = get_models(x_i, y_i, num_traj_per_model)
        seed_x = seeds[x_model - 1]
        seed_y = seeds[y_model - 1]
        data_eval = np.insert(data_eval, insrt, np.concatenate(
            (get_trajectory(seed_x, index_x, trajectory_length),
             get_trajectory_test(seed_y, index_y, trajectory_length))),
                              axis=0)

        if same_set:
            labels_eval.put(insrt, 1)
        else:
            labels_eval.put(insrt, 0)

        indx += 1
        insrt += 1

    np.save(path + d_e + '.npy', data_eval)
    np.save(path + l_e + '.npy', labels_eval)

    del data_eval, labels_eval
    gc.collect()
    print("saved eval pairs")

    return d_t, l_t, d_e, l_e, d_test, labels_test


def logger(baseline, precision_bl, recall_bl, rmse, accuracy, precision, recall):
    print("Baseline Accuracy: ", baseline)
    print("Precision BL: ", precision_bl)
    print("Recall BL: ", recall_bl)
    print("Attack Classifier Accuracy: ", accuracy)
    print("Precision: ", precision)
    print("Recall: ", recall)
    print("Root MSE: ", rmse)
    print("****************************")


def rsme(errors):
    return np.sqrt(gmean(np.square(errors)))


def calc_errors(classifier_predictions, labels_test, threshold, num_predictions):
    errors = []
    for i in range(num_predictions):
        e_i = (labels_test[i] - classifier_predictions[i]) / (labels_test[i] - threshold)
        errors.append(e_i)

    return errors


def baseline_accuracy(labels_test, num_predictions):
    false_positives = 0
    false_negatives = 0
    true_positives = 0
    true_negatives = 0
    for i in range(num_predictions):
        guess = randint(0, 1)

        # if they're the same
        if guess == labels_test[i]:
            if labels_test[i] == 1:
                true_positives += 1
            else:
                true_negatives += 1
        # said out was actually in
        elif guess == 0 and labels_test[i] == 1:
            false_negatives += 1
        elif guess == 1 and labels_test[i] == 0:
            false_positives += 1

    return output_prec_recall(true_positives, true_negatives, false_negatives, false_positives, num_predictions)


def accuracy_report(classifier_predictions, labels_test, threshold, num_predictions):
    false_positives = 0
    false_negatives = 0
    true_positives = 0
    true_negatives = 0
    for i in range(num_predictions):
        if classifier_predictions[i] >= threshold and labels_test[i] == 1:
            true_positives += 1
        elif classifier_predictions[i] < threshold and labels_test[i] == 0:
            true_negatives += 1

        # false negative (classifier is saying out but labels say in)
        elif classifier_predictions[i] < threshold and labels_test[i] == 1:
            false_negatives += 1

        # false positive (classifier is saying in but labels say out)
        elif classifier_predictions[i] >= threshold and labels_test[i] == 0:
            false_positives += 1
    print(
        f"true_positive={true_positives}, true_negative={true_negatives}, false_positive={false_positives}"
        f", false_negative={false_negatives}")
    return output_prec_recall(true_positives, true_negatives, false_negatives, false_positives, num_predictions)


def output_prec_recall(tp, tn, fn, fp, total):
    num_correct = tp + tn

    acc = num_correct / total
    if (tp + fp) == 0:
        prec = -1
    else:
        prec = tp / (tp + fp)
    if (tp + fn) == 0:
        recall = -1
    else:
        recall = tp / (tp + fn)

    return round(acc, 3), round(prec, 3), round(recall, 3)


def generate_metrics(classifier_predictions, labels_test, threshold, num_predictions):
    accuracy, precision, recall = accuracy_report(
        classifier_predictions, labels_test, threshold, num_predictions)
    
    accuracy_bl, precision_bl, recall_bl = baseline_accuracy(labels_test, num_predictions)
    RMSE_e_i = rsme(calc_errors(classifier_predictions, labels_test, threshold, num_predictions))

    logger(accuracy_bl, precision_bl, recall_bl, RMSE_e_i, accuracy, precision, recall)
    return accuracy_bl, precision_bl, recall_bl, RMSE_e_i, accuracy, precision, recall


def train_classifier(xgb_train, xgb_eval, max_depth=20):
    num_round = 150
    param = {'eta': '0.2',
             'n_estimators': '5000',
             'max_depth': max_depth,
             'objective': 'reg:logistic',
             'eval_metric': ['logloss', 'error', 'rmse']}

    watch_list = [(xgb_eval, 'eval'), (xgb_train, 'train')]
    evals_result = {}
    print("training classifier")
    return xgb.train(param, xgb_train, num_round, watch_list, evals_result=evals_result)


def train_attack_model_v2(environment, threshold, trajectory_length, seeds, attack_model_size, test_size, timesteps,
                          dimension):
    path = "tmp_plks/"
    d_t, l_t, d_e, l_e, d_test, labels_test = create_sets(seeds, attack_model_size, timesteps, trajectory_length,
                                                          test_size, dimension)

    # xgb_t, xgb_e, xgb_test = create_xgb_train_test_eval(d_t, l_t, d_e, l_e, d_test)
    attack_classifier = train_classifier(xgb.DMatrix(np.load(path + d_t + '.npy'), label=np.load(path + l_t + '.npy')),
                                         xgb.DMatrix(np.load(path + d_e + '.npy'), label=np.load(path + l_e + '.npy')))

    print("training finished --> generating predictions")
    xgb_testing = xgb.DMatrix(np.load(path + d_test + '.npy'))
    classifier_predictions = attack_classifier.predict(xgb_testing)

    cleanup([d_t, l_t, d_e, l_e, d_test], ["1"])

    print_experiment(environment, len(seeds), threshold, trajectory_length,
                     attack_model_size)

    return generate_metrics(classifier_predictions, labels_test, threshold, test_size)

def get_pairs_max_traj_len(attack_path, state_dim, action_dim, device, args):
    """
    Let's get the maximum length for both positive/negative test/train trajectories.
    This is done for padding purposes.
    """
    print("getting maximum trajectories length...")
    train_traj_lens = []
    test_traj_lens = []
    train_test_seeds = []
    for label in [0, 1]:
        train_test_seeds.append(get_seeds_train_pairs(label, args.seed))
        train_test_seeds.append(get_seeds_test_pairs(label, args.seed))

    for train_seed, test_seed in train_test_seeds:
        # loading buffers to get trajectories lengths
        buffer_name_train = f"{args.buffer_name}_{args.env}_{train_seed}"
        _, _, train_trajectories_end_index = get_buffer_properties(
            buffer_name_train, attack_path, state_dim, action_dim, device, args, train_seed)

        # Maximum trajectory length is calculated for padding purposes
        train_traj_lens.append(compute_max_trajectory_length(train_trajectories_end_index))

        # BCQ output
        buffer_name_test = f"target_{args.buffer_name}_{args.env}_{test_seed}"
        _, _, test_trajectories_end_index = get_buffer_properties(
            buffer_name_test , attack_path, state_dim, action_dim, device, args, test_seed)
        
        # Maximum trajectory length is calculated for padding purposes
        test_traj_lens.append(compute_max_trajectory_length(test_trajectories_end_index))

    return max(test_traj_lens), max(train_traj_lens)


def train_attack_model_v3(attack_path, state_dim, action_dim, device, args):
    
    if args.correlation == DECORRELATED:
        # In decorrelated mode, we use the given max_traj_len as the maximum trajectory length
        test_padding_len = train_padding_len = args.max_traj_len
    else:
        # In correlated mode, we need to load existing trajectories, and find their maximum length
        test_padding_len, train_padding_len = get_pairs_max_traj_len(
            attack_path, state_dim, action_dim, device, args)
    # Pairing train and test trajectories
    # Feeding max length trajectory to be uesd for padding purposes
    # Positive pairs
    train_seed, test_seed = get_seeds_train_pairs(1, args.seed)
    attack_train_positive_data, attack_eval_positive_data = create_pairs(
        attack_path, state_dim, action_dim, device, args, 1,
        train_seed, test_seed,
        do_train=True,
        test_padding_len=test_padding_len,
        train_padding_len=train_padding_len
    )
    # Negative pairs
    train_seed, test_seed = get_seeds_train_pairs(0, args.seed)
    attack_train_negative_data, attack_eval_negative_data = create_pairs(
        attack_path, state_dim, action_dim, device, args, 0,
        train_seed, test_seed,
        do_train=True,
        test_padding_len=test_padding_len,
        train_padding_len=train_padding_len
    )

    print("preparing train data for classifier training ...")
    # Instanciating xgboost DMatrix with positive/negative train data
    attack_train_pos_data, attack_train_pos_label = attack_train_positive_data
    attack_train_neg_data, attack_train_neg_label = attack_train_negative_data
    attack_train_data_x = np.vstack((attack_train_pos_data, attack_train_neg_data))
    attack_train_data_y = np.vstack((attack_train_pos_label, attack_train_neg_label))
    classifier_train_data = xgb.DMatrix(attack_train_data_x, attack_train_data_y)

    print("preparing eval data for classifier training ...")
    # Instanciating xgboost DMatrix with positive/negative train data
    attack_eval_pos_data, attack_eval_pos_label = attack_eval_positive_data
    attack_eval_neg_data, attack_eval_neg_label = attack_eval_negative_data
    attack_eval_data_x = np.vstack((attack_eval_pos_data, attack_eval_neg_data))
    attack_eval_data_y = np.vstack((attack_eval_pos_label, attack_eval_neg_label))
    classifier_eval_data = xgb.DMatrix(attack_eval_data_x, attack_eval_data_y)

    # This part is in parallel with the above few lines WRT getting the data to be fed into XGBoost DMatrix
    # comment out if needed!
    # x_input = None
    # with open(f"./{attack_path}/{0}/attack_outputs/traj_based_buffers/eval_{args.out_traj_size}.npy", 'rb') as f:
    #     fsz = os.fstat(f.fileno()).st_size
    #     x_input = np.load(f)
    #     while f.tell() < fsz:
    #         x_input = np.vstack((x_input, np.load(f)))
    # classifier_train_data = xgb.DMatrix(x_input, )  # Note that in this way, the label needs to be extracted from the arrays

    # This part is in parallel with the above few lines WRT getting the data to be fed into XGBoost DMatrix
    # comment out if needed!
    # e_input = None
    # with open(f"./{attack_path}/{0}/attack_outputs/traj_based_buffers/eval_{args.out_traj_size}.npy", 'rb') as f:
    #     fsz = os.fstat(f.fileno()).st_size
    #     e_input = np.load(f)
    #     while f.tell() < fsz:
    #         e_input = np.vstack((e_input, np.load(f)))
    # classifier_eval_data = xgb.DMatrix(e_input) # Note that in this way, the label needs to be extracted from the arrays

    print("classifier training ...")
    attack_classifier = train_classifier(classifier_train_data, classifier_eval_data, max_depth=args.max_depth)
    print("training finished --> generating predictions")
    # Positive pairs
    train_seed, test_seed = get_seeds_test_pairs(1, args.seed)
    attack_train_positive_data, _ = create_pairs(
        attack_path, state_dim, action_dim, device, args, 1,
        train_seed, test_seed,
        do_train=False,
        test_padding_len=test_padding_len,
        train_padding_len=train_padding_len
    )
    # Negative pairs
    train_seed, test_seed = get_seeds_test_pairs(0, args.seed)
    attack_train_negative_data, _ = create_pairs(
        attack_path, state_dim, action_dim, device, args, 0,
        train_seed, test_seed,
        do_train=False,
        test_padding_len=test_padding_len,
        train_padding_len=train_padding_len
    )
    
    final_train_dataset_pos, final_train_dataset_pos_label = attack_train_positive_data
    final_train_dataset_neg, final_train_dataset_neg_label = attack_train_negative_data
    attack_test_data_x = np.vstack((final_train_dataset_pos, final_train_dataset_neg))
    attack_test_data_y = np.vstack((final_train_dataset_pos_label, final_train_dataset_neg_label))
    classifier_test_data = xgb.DMatrix(attack_test_data_x, attack_test_data_y)
    # prediction phase using the trained attack classifier
    classifier_predictions = attack_classifier.predict(classifier_test_data)

    # NOTE: the number of predictions cannot be more than then number of rows in attack_test_data_x
    # Adjusting num_predictions accordingly
    num_rows, num_columns = attack_test_data_x.shape
    num_predictions = args.attack_sizes[0] if args.attack_sizes[0] <= num_rows else num_rows
    print_experiment(args.env, args.seed, args.attack_thresholds, num_predictions, args.attack_sizes)
    return generate_metrics(classifier_predictions, attack_test_data_y, args.attack_thresholds[0], num_predictions)
