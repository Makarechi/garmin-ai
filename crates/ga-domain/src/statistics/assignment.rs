// Shortest augmenting paths and tie ordering adapted from SciPy 1.18.1,
// scipy/optimize/rectangular_lsap/rectangular_lsap.cpp, author PM Larsen.
// The accompanying LICENSE-scipy.txt retains its BSD-3-Clause terms.

use time::Date;

/// Match the maximum number of episode days to distinct same-weekday controls,
/// then minimize total calendar distance with the legacy deterministic tie order.
pub fn match_weekday_controls(episodes: &[Date], controls: &[Date]) -> Vec<(usize, usize)> {
    let penalty = ((episodes.len() + 1) * 57) as f64;
    let cost = |row: usize, column: usize| {
        let Some(control) = controls.get(column) else {
            return penalty;
        };
        let episode = episodes[row];
        let distance = (*control - episode).whole_days().unsigned_abs();
        if episode.weekday() == control.weekday() && (1..=56).contains(&distance) {
            distance as f64 + column as f64 * 1e-7
        } else {
            penalty * 2.0
        }
    };
    let columns = minimum_cost_assignment(episodes.len(), controls.len() + episodes.len(), cost);
    columns
        .into_iter()
        .enumerate()
        .filter(|&(row, column)| column < controls.len() && cost(row, column) < penalty)
        .collect()
}

// This app's matrix is always wide and finite, with one dummy column per row.
// Evaluate its entries on demand so the 200-day query needs linear working memory.
fn minimum_cost_assignment(
    rows: usize,
    columns: usize,
    cost: impl Fn(usize, usize) -> f64,
) -> Vec<usize> {
    let mut row_dual = vec![0.0; rows];
    let mut column_dual = vec![0.0; columns];
    let mut column_for_row = vec![usize::MAX; rows];
    let mut row_for_column = vec![None; columns];
    let mut path = vec![0; columns];
    let mut distance = vec![0.0; columns];
    let mut visited_rows = vec![false; rows];
    let mut visited_columns = vec![false; columns];
    let mut remaining = Vec::with_capacity(columns);

    for current in 0..rows {
        remaining.clear();
        // Reverse order preserves SciPy's identity solution for constant costs.
        remaining.extend((0..columns).rev());
        visited_rows.fill(false);
        visited_columns.fill(false);
        distance.fill(f64::INFINITY);
        let mut row = current;
        let mut minimum = 0.0;
        let sink = loop {
            visited_rows[row] = true;
            let mut lowest = f64::INFINITY;
            let mut chosen = 0;
            for (index, &column) in remaining.iter().enumerate() {
                let reduced = minimum + cost(row, column) - row_dual[row] - column_dual[column];
                if reduced < distance[column] {
                    path[column] = row;
                    distance[column] = reduced;
                }
                if distance[column] < lowest
                    || (distance[column] == lowest && row_for_column[column].is_none())
                {
                    lowest = distance[column];
                    chosen = index;
                }
            }
            minimum = lowest;
            let column = remaining.swap_remove(chosen);
            visited_columns[column] = true;
            match row_for_column[column] {
                None => break column,
                Some(next) => row = next,
            }
        };

        row_dual[current] += minimum;
        for index in 0..rows {
            if visited_rows[index] && index != current {
                row_dual[index] += minimum - distance[column_for_row[index]];
            }
        }
        for column in 0..columns {
            if visited_columns[column] {
                column_dual[column] -= minimum - distance[column];
            }
        }
        let mut column = sink;
        loop {
            let row = path[column];
            row_for_column[column] = Some(row);
            std::mem::swap(&mut column_for_row[row], &mut column);
            if row == current {
                break;
            }
        }
    }
    column_for_row
}
